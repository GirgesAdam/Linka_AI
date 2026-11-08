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
from sqlalchemy.exc import ArgumentError

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
EXPECTED_ATTESTOR_ROLE = "tia_production_attestor"
WRAPPER_SCHEMA = "attestation_private"
WRAPPER_NAME = "cluster_system_identifier"


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
    except (ArgumentError, TypeError, ValueError):
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
    query["gssencmode"] = "disable"
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


def verify_runtime_identity(
    conn: psycopg.Connection[Any],
    expected_role: str = EXPECTED_ATTESTOR_ROLE,
) -> None:
    """Fail closed unless this connection is the dedicated least-privilege Attestor.

    This guard only reads PostgreSQL catalogs and effective privilege predicates.
    It never invokes a SECURITY DEFINER function. The sole approved wrapper is
    called by run_attestation only after this function returns successfully.
    """
    identity_row = conn.execute(
        """
        SELECT
            session_user::text,
            current_user::text,
            attestor_role.rolcanlogin,
            attestor_role.rolsuper,
            attestor_role.rolcreatedb,
            attestor_role.rolcreaterole,
            attestor_role.rolreplication,
            attestor_role.rolbypassrls,
            attestor_role.rolinherit,
            (
                SELECT count(*)
                FROM pg_catalog.pg_auth_members AS membership
                WHERE membership.member = pg_catalog.to_regrole(current_user::text)::oid
            ) AS role_membership_count,
            current_setting('default_transaction_read_only'),
            pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE'),
            pg_catalog.has_function_privilege(
                current_user,
                'pg_catalog.pg_control_system()',
                'EXECUTE'
            ),
            EXISTS (
                SELECT 1
                FROM pg_catalog.aclexplode(
                    COALESCE(
                        control.proacl,
                        pg_catalog.acldefault('f', control.proowner)
                    )
                ) AS acl
                WHERE acl.grantee = pg_catalog.to_regrole(current_user::text)::oid
                  AND acl.privilege_type = 'EXECUTE'
            ) AS direct_control_execute,
            pg_catalog.has_schema_privilege(
                current_user, 'attestation_private', 'USAGE'
            ),
            pg_catalog.has_function_privilege(
                current_user, wrapper.oid, 'EXECUTE'
            ),
            EXISTS (
                SELECT 1
                FROM pg_catalog.aclexplode(
                    COALESCE(
                        wrapper.proacl,
                        pg_catalog.acldefault('f', wrapper.proowner)
                    )
                ) AS acl
                WHERE acl.grantee = 0
                  AND acl.privilege_type = 'EXECUTE'
            ) AS wrapper_public_execute,
            wrapper.prosecdef,
            pg_catalog.pg_get_userbyid(wrapper.proowner),
            wrapper.pronargs,
            wrapper.proconfig
        FROM pg_catalog.pg_roles AS attestor_role
        LEFT JOIN pg_catalog.pg_proc AS control
          ON control.oid = pg_catalog.to_regprocedure(
              'pg_catalog.pg_control_system()'
          )
        LEFT JOIN pg_catalog.pg_namespace AS wrapper_schema
          ON wrapper_schema.nspname = 'attestation_private'
        LEFT JOIN pg_catalog.pg_proc AS wrapper
          ON wrapper.pronamespace = wrapper_schema.oid
         AND wrapper.proname = 'cluster_system_identifier'
         AND wrapper.pronargs = 0
        WHERE attestor_role.rolname = current_user
        """
    ).fetchone()

    if identity_row is None:
        raise AttestationFailure("runtime_identity_unverified")

    (
        session_user,
        current_user,
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
        wrapper_config,
    ) = identity_row

    if session_user != expected_role or current_user != expected_role:
        raise AttestationFailure("runtime_identity_mismatch")

    if not (
        can_login is True
        and is_superuser is False
        and can_create_db is False
        and can_create_role is False
        and can_replicate is False
        and bypass_rls is False
        and inherit_roles is False
        and membership_count == 0
        and str(default_transaction_read_only).lower() == "on"
    ):
        raise AttestationFailure("runtime_role_attributes_unverified")

    if not (
        public_schema_create is False
        and control_effective_execute is False
        and control_direct_execute is False
        and wrapper_schema_usage is True
        and wrapper_effective_execute is True
        and wrapper_public_execute is False
        and wrapper_security_definer is True
        and wrapper_owner == "postgres"
        and wrapper_argument_count == 0
        and wrapper_config == ["search_path=pg_catalog"]
    ):
        raise AttestationFailure("runtime_effective_privilege_guard_failed")

    exposed_functions = conn.execute(
        """
        SELECT
            namespace.nspname,
            procedure.proname,
            procedure.pronargs,
            pg_catalog.pg_get_function_identity_arguments(procedure.oid)
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        WHERE procedure.prosecdef
          AND pg_catalog.has_schema_privilege(
              current_user, namespace.oid, 'USAGE'
          )
          AND pg_catalog.has_function_privilege(
              current_user, procedure.oid, 'EXECUTE'
          )
        ORDER BY namespace.nspname, procedure.proname,
                 pg_catalog.pg_get_function_identity_arguments(procedure.oid)
        """
    ).fetchall()

    allowed_surface = [
        (WRAPPER_SCHEMA, WRAPPER_NAME, 0, ""),
    ]
    if exposed_functions != allowed_surface:
        raise AttestationFailure("unexpected_security_definer_surface")


def run_attestation(raw_url: str | None = None) -> dict[str, Any]:
    """Run all probes on one verified TLS connection in one READ ONLY transaction."""
    if raw_url is None:
        raw_url = os.environ.get("PRODUCTION_DB_READONLY_URL")
    if not raw_url:
        return _failure("production_db_readonly_url_missing")

    try:
        conninfo, mode = connection_details(raw_url)
    except AttestationFailure as exc:
        return _failure(exc.category)
    except (ArgumentError, TypeError, ValueError) as exc:
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

        phase = "runtime_identity_guard"
        verify_runtime_identity(conn)

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
        if is_replica is True:
            raise AttestationFailure("connected_to_read_replica")
        if is_replica is not False:
            raise AttestationFailure("replica_status_unavailable")

        system_row = conn.execute(
            "SELECT attestation_private.cluster_system_identifier()"
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

        info_pid = conn.info.backend_pid
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
            "runtime_identity_verified": True,
            "security_definer_surface_verified": True,
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
    except (psycopg.Error, OSError, TypeError, ValueError) as exc:
        category = "connection_failed" if phase == "connection" else "runtime_probe_failed"
        failure = _failure(category, exc, mode)
    finally:
        if conn is not None and transaction_started:
            try:
                conn.execute("ROLLBACK")
                transaction_started = False
            except (psycopg.Error, OSError, TypeError, ValueError) as exc:
                failure = _failure("rollback_failed", exc, mode)
        if conn is not None:
            try:
                conn.close()
            except (psycopg.Error, OSError, TypeError, ValueError) as exc:
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
