#!/usr/bin/env python3
"""Fail-closed, read-only fingerprint probe for Tia Production.

The connection URL is parsed with SQLAlchemy and is never logged. PostgreSQL
TLS certificate and hostname verification are forced to libpq's verify-full
mode using the system trust store. One psycopg connection and one explicit
READ ONLY transaction are used for every query; the transaction is rolled back.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import psycopg
from sqlalchemy.engine import URL, make_url

REFERENCE = {
    "cluster_system_id": "7666052913346410626",
    "database_oid": "5",
    "database_name": "postgres",
    "schema": "public",
    "postgres_version": "17.6",
    "alembic_revision": ["0096_patient_customer_id"],
}

ALLOWED_DRIVERS = {"postgresql", "postgresql+psycopg"}
SQLSTATE_PATTERN = re.compile(r"^[0-9A-Z]{5}$")


class AttestationFailure(Exception):
    """A safe, non-sensitive failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def connection_mode(url: URL) -> str:
    """Classify the endpoint without returning its hostname or connection URL."""
    host = (url.host or "").lower()
    port = url.port or 5432

    if host.endswith(".pooler.supabase.com"):
        if port == 6543:
            return "Supavisor transaction pooler"
        if port == 5432:
            return "Supavisor session pooler"
        return "Supavisor pooler (port unclassified)"
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return "Supabase direct connection"
    return "Other/unknown endpoint mode"


def connection_details(raw_url: str) -> tuple[str, str]:
    """Parse SQLAlchemy URL and force full TLS certificate/hostname validation."""
    try:
        parsed = make_url(raw_url)
    except Exception:
        raise AttestationFailure("connection_url_parse_failed") from None

    if parsed.drivername not in ALLOWED_DRIVERS:
        raise AttestationFailure("unsupported_postgresql_driver")
    if not all((parsed.username, parsed.password, parsed.host, parsed.database)):
        raise AttestationFailure("connection_url_incomplete")

    mode = connection_mode(parsed)
    query = dict(parsed.query)
    # Replace any weaker URL setting. Never fall back to require/prefer/disable.
    query["sslmode"] = "verify-full"
    query["sslrootcert"] = "system"
    verified_url = parsed.set(drivername="postgresql", query=query)
    return verified_url.render_as_string(hide_password=False), mode


def _sqlstate(exc: BaseException) -> str | None:
    value = getattr(exc, "sqlstate", None)
    if isinstance(value, str) and SQLSTATE_PATTERN.fullmatch(value):
        return value
    return None


def _failure(
    category: str,
    exc: BaseException | None = None,
    mode: str = "UNVERIFIED",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "UNVERIFIED",
        "failure_category": category,
        "connection_mode": mode,
    }
    if exc is not None:
        result["error_type"] = type(exc).__name__
        result["sqlstate"] = _sqlstate(exc)
        if result["sqlstate"] == "42501":
            result["failure_category"] = "database_permission_denied"
    return result


def run_attestation(raw_url: str | None = None) -> dict[str, Any]:
    """Run all probes on one verified TLS connection in one READ ONLY transaction."""
    if raw_url is None:
        raw_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not raw_url:
        return _failure("migration_database_url_missing")

    try:
        conninfo, mode = connection_details(raw_url)
    except AttestationFailure as exc:
        return _failure(exc.category)
    except Exception as exc:
        return _failure("connection_url_parse_failed", exc)

    conn = None
    transaction_started = False
    failure: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    phase = "connection"

    try:
        # Exactly one connection attempt. conninfo is intentionally never logged.
        conn = psycopg.connect(
            conninfo,
            connect_timeout=8,
            application_name="linka-production-db-readonly-attestation",
            autocommit=True,
        )

        phase = "transaction"
        conn.execute("BEGIN READ ONLY")
        transaction_started = True
        conn.execute("SET LOCAL statement_timeout = '5s'")

        phase = "runtime_probe"
        database_row = conn.execute(
            """
            SELECT
                current_database() AS database_name,
                current_schema() AS current_schema,
                current_setting('search_path') AS search_path,
                current_setting('server_version') AS server_version,
                (
                    SELECT oid::text
                    FROM pg_database
                    WHERE datname = current_database()
                ) AS database_oid,
                pg_backend_pid() AS backend_pid,
                pg_is_in_recovery() AS is_replica,
                current_setting('transaction_read_only') AS transaction_read_only
            """
        ).fetchone()
        if database_row is None:
            raise AttestationFailure("database_fingerprint_query_empty")

        (
            database_name,
            schema,
            search_path,
            server_version,
            database_oid,
            backend_pid,
            is_replica,
            transaction_read_only,
        ) = database_row

        if str(transaction_read_only).lower() != "on":
            raise AttestationFailure("read_only_transaction_not_confirmed")
        if not backend_pid or not database_oid:
            raise AttestationFailure("database_fingerprint_incomplete")

        system_row = conn.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()
        if system_row is None or not system_row[0]:
            raise AttestationFailure("cluster_system_id_unavailable")
        system_identifier = str(system_row[0])

        revision_rows = conn.execute(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).fetchall()
        if not revision_rows:
            raise AttestationFailure("alembic_revision_unavailable")
        revisions = [str(row[0]) for row in revision_rows]

        tls_row = conn.execute(
            """
            SELECT ssl, version, cipher, bits
            FROM pg_stat_ssl
            WHERE pid = pg_backend_pid()
            """
        ).fetchone()
        if (
            tls_row is None
            or tls_row[0] is not True
            or not tls_row[1]
            or not tls_row[2]
            or not tls_row[3]
        ):
            raise AttestationFailure("tls_not_verified")

        final_session_row = conn.execute(
            """
            SELECT
                pg_backend_pid(),
                current_setting('transaction_read_only')
            """
        ).fetchone()
        if (
            final_session_row is None
            or int(final_session_row[0]) != int(backend_pid)
            or str(final_session_row[1]).lower() != "on"
        ):
            raise AttestationFailure("single_connection_transaction_not_confirmed")

        info_pid = getattr(getattr(conn, "info", None), "backend_pid", backend_pid)
        if info_pid is not None and int(info_pid) != int(backend_pid):
            raise AttestationFailure("backend_pid_changed")

        identity_match = (
            system_identifier == REFERENCE["cluster_system_id"]
            and str(database_oid) == REFERENCE["database_oid"]
            and str(database_name) == REFERENCE["database_name"]
        )
        metadata_match = (
            str(schema) == REFERENCE["schema"]
            and str(server_version) == REFERENCE["postgres_version"]
            and revisions == REFERENCE["alembic_revision"]
        )

        result = {
            "status": "VERIFIED",
            "verdict": "SAME_DATABASE" if identity_match else "DIFFERENT_DATABASE",
            "baseline_metadata_match": metadata_match,
            "cluster_system_id": system_identifier,
            "database_oid": str(database_oid),
            "database_name": str(database_name),
            "schema": str(schema),
            "search_path": str(search_path),
            "postgres_version": str(server_version),
            "alembic_revision": revisions,
            "connection_mode": mode,
            "is_replica": bool(is_replica),
            "connection_count": 1,
            "backend_pid": int(backend_pid),
            "single_transaction": True,
            "transaction_read_only": True,
            "tls": {
                "enabled": True,
                "version": str(tls_row[1]),
                "cipher": str(tls_row[2]),
                "bits": int(tls_row[3]),
                "certificate_verification": "verify-full",
                "trust_store": "system",
            },
            "rollback_completed": False,
        }
    except AttestationFailure as exc:
        failure = _failure(exc.category, mode=mode)
    except Exception as exc:
        category = "connection_failed" if phase == "connection" else "runtime_probe_failed"
        failure = _failure(category, exc, mode)
    finally:
        if conn is not None and transaction_started:
            try:
                conn.execute("ROLLBACK")
                transaction_started = False
            except Exception as exc:
                failure = _failure("rollback_failed", exc, mode)
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                if failure is None:
                    failure = _failure("connection_close_failed", exc, mode)

    if failure is not None:
        return failure
    if result is None:
        return _failure("attestation_result_missing", mode=mode)

    result["rollback_completed"] = not transaction_started
    if not result["rollback_completed"]:
        return _failure("rollback_not_confirmed", mode=mode)
    return result


def main() -> int:
    result = run_attestation()
    print(json.dumps(result, sort_keys=True))
    if (
        result.get("status") == "VERIFIED"
        and result.get("verdict") == "SAME_DATABASE"
        and result.get("baseline_metadata_match") is True
        and result.get("rollback_completed") is True
    ):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
