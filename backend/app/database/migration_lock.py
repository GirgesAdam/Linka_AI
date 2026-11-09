from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("alembic.runtime.migration")

# Shared by every service and workflow that runs Alembic against this database.
# The hexadecimal ASCII marker "LINKAMIG" makes accidental key reuse unlikely.
ALEMBIC_MIGRATION_LOCK_KEY = 0x4C494E4B414D4947
DEFAULT_LOCK_WAIT_TIMEOUT_SECONDS = 300.0
LOCK_POLL_INTERVAL_SECONDS = 1.0


class MigrationAdvisoryLockTimeout(TimeoutError):
    """Raised when another Alembic process holds the shared migration lock."""


def acquire_migration_lock(
    connection: Connection,
    *,
    timeout_seconds: float = DEFAULT_LOCK_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = LOCK_POLL_INTERVAL_SECONDS,
    lock_key: int = ALEMBIC_MIGRATION_LOCK_KEY,
) -> None:
    """Acquire the database-wide session lock and leave SQLAlchemy transaction-idle.

    A session-level advisory lock survives COMMIT, ROLLBACK, and PostgreSQL
    AUTOCOMMIT DDL boundaries. Each try-lock statement autobegins a SQLAlchemy
    transaction, so commit it before Alembic starts its own transaction.
    """
    if connection.in_transaction():
        raise RuntimeError("Alembic migration lock requires an idle SQLAlchemy connection.")
    if not isinstance(lock_key, int) or not -(2**63) <= lock_key < 2**63:
        raise ValueError("PostgreSQL advisory lock key must be a signed 64-bit integer.")
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("Migration lock timeout must be a finite non-negative number.")
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise ValueError("Migration lock polling interval must be a finite positive number.")

    deadline = time.monotonic() + timeout_seconds
    while True:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            ).scalar_one()
        )
        # SQLAlchemy 2.0 autobegins on the SELECT. A commit clears that
        # transaction wrapper without releasing a session-level advisory lock.
        connection.commit()
        if acquired:
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MigrationAdvisoryLockTimeout(
                "Timed out after "
                f"{timeout_seconds:g} seconds waiting for the shared PostgreSQL "
                f"Alembic migration lock (key={lock_key})."
            )
        time.sleep(min(poll_interval_seconds, remaining))


def release_migration_lock(
    connection: Connection,
    *,
    lock_key: int = ALEMBIC_MIGRATION_LOCK_KEY,
) -> None:
    """Release the session lock without committing any pending migration work."""
    # The Alembic transaction context normally rolls back before this point.
    # Keep this defensive rollback so an interrupted migration is never
    # accidentally committed by the transaction opened for pg_advisory_unlock.
    if connection.invalidated:
        # The server-side session is gone, so PostgreSQL has already released it.
        return
    if connection.in_transaction():
        connection.rollback()

    unlocked = bool(
        connection.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": lock_key},
        ).scalar_one()
    )
    connection.commit()
    if not unlocked:
        raise RuntimeError(
            "PostgreSQL did not release the shared Alembic migration lock "
            f"(key={lock_key}); the lock was not held by this session."
        )


@contextmanager
def migration_session_lock(
    connection: Connection,
    *,
    timeout_seconds: float = DEFAULT_LOCK_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = LOCK_POLL_INTERVAL_SECONDS,
    lock_key: int = ALEMBIC_MIGRATION_LOCK_KEY,
) -> Iterator[None]:
    """Hold the shared PostgreSQL session lock for the complete Alembic run."""
    acquire_migration_lock(
        connection,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        lock_key=lock_key,
    )
    migration_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        migration_error = exc
        raise
    finally:
        try:
            release_migration_lock(connection, lock_key=lock_key)
        except Exception:
            if migration_error is None:
                raise
            # Preserve the original migration failure. The enclosing
            # NullPool connection context will close the PostgreSQL session,
            # which releases any session-level lock still held by that session.
            logger.exception(
                "Failed to explicitly release the Alembic migration lock "
                "after a migration error; closing the connection will release it."
            )
