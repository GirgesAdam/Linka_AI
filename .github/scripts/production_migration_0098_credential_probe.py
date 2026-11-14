"""Protected, strictly read-only existing-secret compatibility probe for Migration 0098.

No application imports, migrations, database writes, or credential output.
This tool executes only when separately dispatched and approved on main.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile

EXPECTED_MAIN_REF = "refs/heads/main"
EXPECTED_REVISION = "0097_member_page_permissions"
EXPECTED_PG_VERSION = 170006
EXPECTED_DATABASE_OID = "5"
RUNNER_PATH = ".github/scripts/production_migration_0098.py"
MIGRATION_PATH = "backend/alembic/versions/0098_doctor_laser_weekday_exclusions.py"
APPROVED_MIGRATION_SHA256 = "0d1c63717653e5b7f56e91698279b8896636c014601a75f75cf0794a9f77d100"
CODES = frozenset({
    "SOURCE_GUARD_FAILED",
    "MIGRATION_HASH_MISMATCH",
    "MIGRATION_RUNNER_IMPORT_FAILED",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_IDENTITY_REJECTED",
    "CA_CERT_MISSING",
    "CA_CERT_INVALID",
    "DATABASE_IDENTITY_MISMATCH",
    "REVISION_MISMATCH",
    "READ_ONLY_NOT_CONFIRMED",
    "TLS_NOT_CONFIRMED",
    "ROLLBACK_FAILED",
    "DATABASE_CONNECTION_OR_QUERY_FAILED",
})


class ProbeFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def check_migration_hash(blob: bytes) -> None:
    if hashlib.sha256(blob).hexdigest() != APPROVED_MIGRATION_SHA256:
        raise ProbeFailure("MIGRATION_HASH_MISMATCH")


def source_guard(repo: Path) -> None:
    if (os.getenv("GITHUB_REF") != EXPECTED_MAIN_REF or
            os.getenv("GITHUB_RUN_ATTEMPT") != "1"):
        raise ProbeFailure("SOURCE_GUARD_FAILED")
    sha = os.getenv("GITHUB_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ProbeFailure("SOURCE_GUARD_FAILED")
    try:
        local = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10
        ).stdout.decode("ascii").strip()
        remote = subprocess.run(
            ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"], cwd=repo,
            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=15
        ).stdout.decode("ascii").split()
        if local != sha or remote != [sha, "refs/heads/main"]:
            raise ProbeFailure("SOURCE_GUARD_FAILED")
        check_migration_hash((repo / MIGRATION_PATH).read_bytes())
    except ProbeFailure:
        raise
    except Exception:
        raise ProbeFailure("SOURCE_GUARD_FAILED") from None


def load_runner(repo: Path):
    try:
        spec = importlib.util.spec_from_file_location("_approved_0098_runner", repo / RUNNER_PATH)
        if spec is None or spec.loader is None:
            raise ValueError()
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if module.APPROVED_MIGRATION_SHA256 != APPROVED_MIGRATION_SHA256:
            raise ValueError()
        return module
    except Exception:
        raise ProbeFailure("MIGRATION_RUNNER_IMPORT_FAILED") from None


def ca_file(runner, ca_pem: str, directory: Path) -> Path:
    if not ca_pem:
        raise ProbeFailure("CA_CERT_MISSING")
    try:
        # Syntax check complements actual libpq verify-full chain/hostname checks.
        ssl.PEM_cert_to_DER_cert(ca_pem.strip())
        return runner._write_ca(ca_pem, directory)
    except Exception:
        raise ProbeFailure("CA_CERT_INVALID") from None


def inspect_readonly(connection) -> None:
    started = False
    try:
        connection.execute("BEGIN READ ONLY")
        started = True
        connection.execute("SET LOCAL statement_timeout = '5s'")
        row = connection.execute(
            "SELECT current_database(), current_setting('server_version_num')::integer, "
            "current_setting('transaction_read_only'), pg_catalog.pg_is_in_recovery(), "
            "session_user::text"
        ).fetchone()
        if (row is None or row[0] != "postgres" or row[1] != EXPECTED_PG_VERSION or
                row[3] is not False or row[4] != "postgres"):
            raise ProbeFailure("DATABASE_IDENTITY_MISMATCH")
        if str(row[2]).lower() != "on":
            raise ProbeFailure("READ_ONLY_NOT_CONFIRMED")

        oid = connection.execute(
            "SELECT oid::text FROM pg_catalog.pg_database "
            "WHERE datname = pg_catalog.current_database()"
        ).fetchone()
        public = connection.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname = 'public'"
        ).fetchone()
        if oid is None or str(oid[0]) != EXPECTED_DATABASE_OID or public != ("public",):
            raise ProbeFailure("DATABASE_IDENTITY_MISMATCH")

        revisions = connection.execute(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).fetchall()
        if revisions != [(EXPECTED_REVISION,)]:
            raise ProbeFailure("REVISION_MISMATCH")

        # libpq has already verified CA and pooler hostname (sslmode=verify-full).
        # pg_stat_ssl describes the pooler-to-database leg, not client-to-pooler TLS.
    finally:
        if started:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                raise ProbeFailure("ROLLBACK_FAILED") from None


def probe(repo: Path) -> None:
    source_guard(repo)
    runner = load_runner(repo)
    raw_url = os.getenv("PRODUCTION_MIGRATION_DATABASE_URL", "")
    if not raw_url:
        raise ProbeFailure("CREDENTIAL_MISSING")
    try:
        runner.assert_production_url(raw_url)
    except Exception:
        raise ProbeFailure("CREDENTIAL_IDENTITY_REJECTED") from None
    with tempfile.TemporaryDirectory(prefix="linka-0098-compat-") as folder:
        root = ca_file(runner, os.getenv("PRODUCTION_MIGRATION_DB_CA_CERT", ""),
                       Path(folder))
        from sqlalchemy.engine import make_url
        import psycopg
        # No application startup or migration framework imports.
        u = make_url(raw_url)
        conn = None
        try:
            conn = psycopg.connect(
                host=u.host, port=u.port, user=u.username, password=u.password,
                dbname=u.database, sslmode="verify-full", sslrootcert=str(root),
                connect_timeout=10, application_name="linka_0098_compatibility_probe",
                autocommit=True
            )
            inspect_readonly(conn)
        except ProbeFailure:
            raise
        except Exception:
            # Never emit raw DB exceptions: they may contain hostnames or secrets.
            raise ProbeFailure("DATABASE_CONNECTION_OR_QUERY_FAILED") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    raise ProbeFailure("DATABASE_CONNECTION_OR_QUERY_FAILED") from None


def main() -> int:
    try:
        mode = sys.argv[1] if len(sys.argv) == 2 else ""
        if mode not in ("source", "probe"):
            raise ProbeFailure("SOURCE_GUARD_FAILED")
        repo = Path(os.getenv("GITHUB_WORKSPACE", ".")).resolve()
        if mode == "source":
            source_guard(repo)
            print("0098_COMPATIBILITY_SOURCE_GUARD_PASS")
        else:
            probe(repo)
            print("PASS — Existing credential compatible with approved 0098 Runner")
            print("0098_EXISTING_CREDENTIAL_COMPATIBILITY_PASS")
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, ProbeFailure) and exc.code in CODES else "DATABASE_CONNECTION_OR_QUERY_FAILED"
        print("::error::0098_COMPATIBILITY_" + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
