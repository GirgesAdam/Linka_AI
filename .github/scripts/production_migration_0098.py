"""Fail-closed, single-revision production migration 0098 runner.

No database connection in source-only preflight. Production execution requires the
protected GitHub environment, explicit owner approval and exact-source evidence.
Never prints a credential, connection URL, record or SQL statement on failure.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REVISION_BEFORE = "0097_member_page_permissions"
REVISION_AFTER = "0098_doctor_laser_weekday_exclusions"
MIGRATION_PATH = "backend/alembic/versions/0098_doctor_laser_weekday_exclusions.py"
APPROVED_MIGRATION_SHA256 = "0d1c63717653e5b7f56e91698279b8896636c014601a75f75cf0794a9f77d100"
PROJECT_REF = "ycuxjlkhnubztgqmhtom"
APPROVED_HOST = "aws-1-eu-west-3.pooler.supabase.com"
EXPECTED_DB = "postgres"
EXPECTED_USER = "postgres." + PROJECT_REF
TABLE = "doctor_laser_device_weekday_exclusions"
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "60s"

EXPECTED_COLUMNS = {
    "id", "created_at", "updated_at", "workspace_id", "doctor_id",
    "branch_id", "weekday", "device_key",
}
EXPECTED_CONSTRAINTS = {
    "doctor_laser_device_weekday_exclusions_pkey": "p",
    "fk_doctor_laser_weekday_assignment": "f",
    "fk_doctor_laser_weekday_device": "f",
    "ck_doctor_laser_weekday_valid": "c",
    "uq_doctor_laser_weekday_exclusion": "u",
}
EXPECTED_EXPLICIT_INDEXES = {
    f"ix_{TABLE}_workspace_id",
    f"ix_{TABLE}_doctor_id",
    f"ix_{TABLE}_branch_id",
}
# The migration uses PostgreSQL transactional DDL only. Do not add AUTOCOMMIT DDL.
# The exact upgrade function is read from the hash-attested Git checkout.


class GateFailure(ValueError):
    """An approval, identity or schema invariant was not satisfied."""


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if result.returncode != 0:
        raise GateFailure("GIT_SOURCE_VERIFICATION_FAILED")
    return result.stdout


def validate_source(
    *,
    repo: Path,
    github_ref: str,
    github_sha: str,
    expected_main_sha: str,
    expected_migration_sha256: str,
    expected_current_revision: str,
    run_attempt: str,
    check_remote_main: bool = True,
) -> str:
    if github_ref != "refs/heads/main" or run_attempt != "1":
        raise GateFailure("DISPATCH_SOURCE_OR_RERUN_REJECTED")
    if not re.fullmatch(r"[0-9a-f]{40}", github_sha):
        raise GateFailure("INVALID_DISPATCH_SHA")
    if github_sha != expected_main_sha:
        raise GateFailure("UNAPPROVED_MAIN_SHA")
    if expected_current_revision != REVISION_BEFORE:
        raise GateFailure("UNAPPROVED_STARTING_REVISION")
    if expected_migration_sha256.lower() != APPROVED_MIGRATION_SHA256:
        raise GateFailure("UNAPPROVED_MIGRATION_HASH")
    if _git(repo, "rev-parse", "HEAD").decode().strip() != github_sha:
        raise GateFailure("CHECKOUT_SHA_MISMATCH")
    if check_remote_main:
        remote = _git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/main").decode()
        entries = remote.split()
        if len(entries) != 2 or entries[0] != github_sha or entries[1] != "refs/heads/main":
            raise GateFailure("REMOTE_MAIN_MOVED_REQUIRES_NEW_APPROVAL")
    blob = _git(repo, "show", f"{github_sha}:{MIGRATION_PATH}")
    if hashlib.sha256(blob).hexdigest() != APPROVED_MIGRATION_SHA256:
        raise GateFailure("MIGRATION_GIT_BLOB_HASH_MISMATCH")
    return hashlib.sha256(blob).hexdigest()


def assert_production_url(raw: str) -> None:
    """Pin the authorized Supabase session-pooler endpoint, not just a DB name."""
    from sqlalchemy.engine import make_url

    try:
        url = make_url(raw)
        allowed = (
            url.drivername == "postgresql+psycopg"
            and url.host == APPROVED_HOST
            and url.port == 5432
            and url.username == EXPECTED_USER
            and url.database == EXPECTED_DB
            and bool(url.password)
        )
        # Do not allow service/hostaddr/options or SSL flags to override host verification.
        query = dict(url.query)
        allowed = allowed and not (set(query) - {"sslmode"})
        allowed = allowed and query.get("sslmode", "verify-full") == "verify-full"
    except Exception:
        allowed = False
    if not allowed:
        raise GateFailure("PRODUCTION_DATABASE_URL_IDENTITY_REJECTED")


def _write_ca(pem: str, output_directory: Path) -> Path:
    """CA exists only within the ephemeral approved runner, never in the repo."""
    if not (pem.startswith("-----BEGIN CERTIFICATE-----") and
            pem.rstrip().endswith("-----END CERTIFICATE-----") and
            len(pem) < 32768):
        raise GateFailure("DATABASE_CA_CERTIFICATE_NOT_CONFIGURED")
    filename = output_directory / "supabase-0098-ca.pem"
    with filename.open("x", encoding="ascii") as handle:
        handle.write(pem)
    filename.chmod(0o600)
    return filename


def _single_version(connection) -> str:
    from sqlalchemy import text

    revisions = connection.execute(
        text("SELECT version_num FROM public.alembic_version")
    ).scalars().all()
    if len(revisions) != 1:
        raise GateFailure("ALEMBIC_REVISION_CARDINALITY_INVALID")
    return str(revisions[0])


def _count(connection, sql: str, **args) -> int:
    from sqlalchemy import text

    return int(connection.execute(text(sql), args).scalar_one())


def _required_parent_constraints(connection) -> None:
    from sqlalchemy import text

    for schema_table, constraint, expected_cols in (
        ("doctor_branches", "uq_doctor_branches_assignment",
         "ARRAY['workspace_id','doctor_id','branch_id']::text[]"),
        ("clinic_laser_devices", "uq_clinic_laser_devices_workspace_device_key",
         "ARRAY['workspace_id','device_key']::text[]"),
    ):
        # Verify *actual* column order, not just an untrusted constraint name.
        count = _count(
            connection,
            """SELECT count(*) FROM pg_constraint c
               JOIN pg_class t ON t.oid=c.conrelid
               JOIN pg_namespace n ON n.oid=t.relnamespace
               WHERE n.nspname='public' AND t.relname=:table
                 AND c.conname=:name AND c.contype IN ('p','u')
                 AND c.convalidated
                 AND ARRAY(
                    SELECT a.attname::text
                    FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
                    ORDER BY k.ord
                 ) = """ + expected_cols,
            table=schema_table, name=constraint,
        )
        if count != 1:
            raise GateFailure("REQUIRED_PARENT_UNIQUE_CONSTRAINT_MISSING")


def _existing_tables(connection) -> set[str]:
    from sqlalchemy import text

    return set(connection.execute(text(
        """SELECT n.nspname || '.' || c.relname
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE c.relkind IN ('r','p')
           AND n.nspname NOT IN ('pg_catalog','information_schema')
           AND n.nspname NOT LIKE 'pg_toast%'"""
    )).scalars())


def _existing_indexes(connection) -> set[str]:
    from sqlalchemy import text

    return set(connection.execute(text(
        """SELECT n.nspname || '.' || idx.relname
           FROM pg_index i
           JOIN pg_class idx ON idx.oid=i.indexrelid
           JOIN pg_namespace n ON n.oid=idx.relnamespace
           WHERE n.nspname NOT IN ('pg_catalog','information_schema')
           AND n.nspname NOT LIKE 'pg_toast%'"""
    )).scalars())


def _business_row_counts(connection) -> dict[str, int]:
    from sqlalchemy import text

    # Aggregate counts only; never read or log individual clinic records.
    return {
        name: int(connection.execute(text(f"SELECT count(*) FROM public.{name}")).scalar_one())
        for name in ("appointments", "doctors", "doctor_branches", "clinic_laser_devices")
    }


def validate_before_locked(connection) -> tuple[set[str], set[str], dict[str, int]]:
    from sqlalchemy import text

    identity = connection.execute(text(
        "SELECT current_database(), current_setting('server_version_num')::integer"
    )).one()
    if identity[0] != EXPECTED_DB or not (170006 <= identity[1] < 180000):
        raise GateFailure("DATABASE_NAME_OR_VERSION_INVALID")
    roles = set(connection.execute(text(
        "SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated','supabase_admin')"
    )).scalars())
    if not {"anon", "authenticated", "supabase_admin"}.issubset(roles):
        raise GateFailure("SUPABASE_REQUIRED_ROLES_MISSING")
    if _single_version(connection) != REVISION_BEFORE:
        raise GateFailure("STARTING_REVISION_MISMATCH")
    exists = connection.execute(text(
        "SELECT to_regclass('public.doctor_laser_device_weekday_exclusions')"
    )).scalar_one()
    if exists is not None:
        raise GateFailure("TARGET_TABLE_ALREADY_PRESENT")
    _required_parent_constraints(connection)
    return _existing_tables(connection), _existing_indexes(connection), _business_row_counts(connection)


def validate_after_locked(connection, old_tables: set[str], old_indexes: set[str],
                          old_counts: dict[str, int]) -> None:
    from sqlalchemy import text

    if _single_version(connection) != REVISION_AFTER:
        raise GateFailure("POST_MIGRATION_REVISION_MISMATCH")
    cols = set(connection.execute(text(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema='public' AND table_name=:table"""
    ), {"table": TABLE}).scalars())
    if cols != EXPECTED_COLUMNS:
        raise GateFailure("POST_MIGRATION_COLUMNS_INVALID")
    crows = connection.execute(text(
        """SELECT c.conname,c.contype::text,
                  CASE WHEN c.contype='f' THEN c.confdeltype::text ELSE '' END
           FROM pg_constraint c
           WHERE c.conrelid=to_regclass('public.doctor_laser_device_weekday_exclusions')"""
    )).all()
    if {name: kind for name, kind, _ in crows} != EXPECTED_CONSTRAINTS:
        raise GateFailure("POST_MIGRATION_CONSTRAINT_SET_INVALID")
    if any(delete_action != "c" for name, kind, delete_action in crows if kind == "f"):
        raise GateFailure("POST_MIGRATION_CASCADE_INVALID")
    fk_rows = connection.execute(text(
        """SELECT c.conname, parent_ns.nspname, parent.relname,
                  ARRAY(SELECT a.attname::text
                        FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum,ord)
                        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
                        ORDER BY k.ord),
                  ARRAY(SELECT a.attname::text
                        FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum,ord)
                        JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.attnum
                        ORDER BY k.ord),
                  c.convalidated
           FROM pg_constraint c
           JOIN pg_class parent ON parent.oid=c.confrelid
           JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace
           WHERE c.conrelid=to_regclass('public.doctor_laser_device_weekday_exclusions')
           AND c.contype='f'"""
    )).all()
    actual_fk = {
        name: (schema, table, tuple(src), tuple(dst), valid)
        for name, schema, table, src, dst, valid in fk_rows
    }
    expected_fk = {
        "fk_doctor_laser_weekday_assignment": (
            "public", "doctor_branches",
            ("workspace_id", "doctor_id", "branch_id"),
            ("workspace_id", "doctor_id", "branch_id"), True,
        ),
        "fk_doctor_laser_weekday_device": (
            "public", "clinic_laser_devices",
            ("workspace_id", "device_key"),
            ("workspace_id", "device_key"), True,
        ),
    }
    if actual_fk != expected_fk:
        raise GateFailure("POST_MIGRATION_FOREIGN_KEY_TARGET_OR_COLUMNS_INVALID")
    indexes = connection.execute(text(
        """SELECT idx.relname,i.indisvalid
           FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
           WHERE i.indrelid=to_regclass('public.doctor_laser_device_weekday_exclusions')"""
    )).all()
    if len(indexes) != 5 or not all(valid for _, valid in indexes):
        raise GateFailure("POST_MIGRATION_INDEXES_INVALID")
    if not EXPECTED_EXPLICIT_INDEXES.issubset({name for name, _ in indexes}):
        raise GateFailure("POST_MIGRATION_EXPLICIT_INDEX_MISSING")
    rls = connection.execute(text(
        """SELECT relrowsecurity FROM pg_class
           WHERE oid=to_regclass('public.doctor_laser_device_weekday_exclusions')"""
    )).scalar_one()
    if not rls:
        raise GateFailure("POST_MIGRATION_RLS_DISABLED")
    for role in ("anon", "authenticated"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            allowed = connection.execute(text(
                "SELECT has_table_privilege(:role, :relation, :privilege)"
            ), {"role": role, "relation": "public."+TABLE, "privilege": privilege}).scalar_one()
            if allowed:
                raise GateFailure("POST_MIGRATION_BROWSER_ROLE_PRIVILEGE_EXPOSED")
    if _existing_tables(connection) != old_tables | {"public." + TABLE}:
        raise GateFailure("UNEXPECTED_EXISTING_TABLE_CHANGE")
    new_index_set = _existing_indexes(connection) - old_indexes
    if new_index_set != {"public." + name for name, _ in indexes}:
        raise GateFailure("UNEXPECTED_EXISTING_INDEX_CHANGE")
    if _business_row_counts(connection) != old_counts:
        raise GateFailure("UNEXPECTED_BUSINESS_ROW_COUNT_CHANGE")


def execute_production(repo: Path, raw_url: str, ca_pem: str) -> None:
    """Single lock + single transactional upgrade, with verification before COMMIT."""
    from alembic.runtime.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    backend = repo / "backend"
    sys.path.insert(0, str(backend))
    from app.database.migration_lock import migration_session_lock
    from app.database.migration_timeouts import migration_timeout_guard, apply_transaction_local_timeouts

    assert_production_url(raw_url)
    with tempfile.TemporaryDirectory(prefix="linka-0098-ca-") as temp_dir:
        cert_path = _write_ca(ca_pem, Path(temp_dir))
        # TLS hostname & certificate validation are mandatory. The protected
        # secret is never printed, and sslmode is overridden to verify-full.
        engine = create_engine(
            raw_url,
            poolclass=NullPool,
            connect_args={"sslmode": "verify-full", "sslrootcert": str(cert_path),
                          "connect_timeout": 10, "application_name": "linka_migrate_0098"},
        )
        try:
            with engine.connect() as connection:
                with migration_session_lock(connection):
                    with migration_timeout_guard(connection):
                        with connection.begin():
                            apply_transaction_local_timeouts(connection)
                            connection.exec_driver_sql("SET LOCAL search_path TO public, pg_catalog")
                            before_tables, before_indexes, before_counts = validate_before_locked(connection)
                            source = repo / MIGRATION_PATH
                            spec = importlib.util.spec_from_file_location("_approved_0098", source)
                            if spec is None or spec.loader is None:
                                raise GateFailure("MIGRATION_IMPORT_FAILED")
                            module = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(module)
                            if module.revision != REVISION_AFTER or module.down_revision != REVISION_BEFORE:
                                raise GateFailure("MIGRATION_MODULE_REVISION_MISMATCH")
                            context = MigrationContext.configure(
                                connection, opts={"version_table_schema": "public", "transactional_ddl": True}
                            )
                            with Operations.context(context):
                                module.upgrade()
                            result = connection.execute(text(
                                "UPDATE public.alembic_version SET version_num=:new "
                                "WHERE version_num=:old"
                            ), {"new": REVISION_AFTER, "old": REVISION_BEFORE})
                            if result.rowcount != 1:
                                raise GateFailure("ALEMBIC_VERSION_UPDATE_RACE")
                            validate_after_locked(connection, before_tables, before_indexes, before_counts)
                        # New transaction, still holding the same session lock.
                        with connection.begin():
                            if _single_version(connection) != REVISION_AFTER:
                                raise GateFailure("POST_COMMIT_REVISION_MISMATCH")
        finally:
            engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("source", "execute"))
    args = parser.parse_args()
    try:
        workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
        sha = validate_source(
            repo=workspace, github_ref=os.environ.get("GITHUB_REF", ""),
            github_sha=os.environ.get("GITHUB_SHA", ""),
            expected_main_sha=os.environ.get("EXPECTED_MAIN_SHA", ""),
            expected_migration_sha256=os.environ.get("EXPECTED_MIGRATION_SHA256", ""),
            expected_current_revision=os.environ.get("EXPECTED_CURRENT_REVISION", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        )
        if args.mode == "source":
            print("0098_SOURCE_GUARD_PASS")
            print("APPROVED_MAIN_SHA=" + os.environ["GITHUB_SHA"])
            print("APPROVED_MIGRATION_SHA256=" + sha)
            return 0

        if os.environ.get("ENVIRONMENT") != "production":
            raise GateFailure("PRODUCTION_ENVIRONMENT_GUARD_FAILED")
        raw_url = os.environ.get("PRODUCTION_MIGRATION_0098_DATABASE_URL", "")
        ca_pem = os.environ.get("PRODUCTION_MIGRATION_DB_CA_CERT", "")
        assert_production_url(raw_url)
        execute_production(workspace, raw_url, ca_pem)
        print("0098_PRODUCTION_MIGRATION_AND_ATTESTATION_PASS")
        return 0
    except GateFailure as exc:
        # Deliberately emit only fixed uppercase control failure codes.
        code = str(exc)
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "0098_PRECONDITION_FAILED"
        print("::error::" + code, file=sys.stderr)
        return 1
    except Exception:
        print("::error::0098_EXECUTION_FAILED_REVIEW_REQUIRED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
