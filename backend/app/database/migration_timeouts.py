from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.engine import Connection

logger = logging.getLogger("alembic.runtime.migration")

MIGRATION_LOCK_TIMEOUT = "5s"
MIGRATION_STATEMENT_TIMEOUT = "60s"


@dataclass
class _AutocommitTimeoutState:
    previous: tuple[str, str] | None = None
    active: bool = False
    reapply_local: bool = False
    cleanup_error: Exception | None = None
    migration_error: BaseException | None = None


def _set_transaction_local_timeouts(connection: Connection) -> None:
    connection.exec_driver_sql(
        f"SET LOCAL lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'"
    )
    connection.exec_driver_sql(
        f"SET LOCAL statement_timeout = '{MIGRATION_STATEMENT_TIMEOUT}'"
    )


def apply_transaction_local_timeouts(connection: Connection) -> None:
    """Apply DDL timeouts to the active Alembic transaction on its own connection."""
    if not connection.in_transaction():
        raise RuntimeError(
            "Alembic DDL timeouts require an active transaction on the migration connection."
        )

    _set_transaction_local_timeouts(connection)


@contextmanager
def migration_timeout_guard(connection: Connection) -> Iterator[None]:
    """Keep DDL timeouts effective across Alembic's AUTOCOMMIT migration blocks.

    Normal migration work uses SET LOCAL. PostgreSQL cannot apply SET LOCAL while
    Alembic is in AUTOCOMMIT mode, so that narrow interval temporarily uses the
    same values at session scope on this dedicated connection. The previous
    session values are restored before Alembic returns to a normal transaction;
    the next transaction then receives SET LOCAL again.
    """
    state = _AutocommitTimeoutState()

    def on_begin(conn: Connection) -> None:
        if conn.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
            if state.active:
                raise RuntimeError("Nested Alembic AUTOCOMMIT timeout guards are unsupported.")

            previous = conn.exec_driver_sql(
                "SELECT current_setting('lock_timeout'), "
                "current_setting('statement_timeout')"
            ).one()
            state.previous = (str(previous[0]), str(previous[1]))
            state.active = True

            conn.exec_driver_sql(
                f"SET lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'"
            )
            conn.exec_driver_sql(
                f"SET statement_timeout = '{MIGRATION_STATEMENT_TIMEOUT}'"
            )
        elif state.reapply_local:
            # SQLAlchemy's "begin" event fires before Connection.in_transaction()
            # becomes true, so use the private event-only helper here.
            _set_transaction_local_timeouts(conn)
            state.reapply_local = False

    def restore_autocommit_settings(conn: Connection) -> None:
        if not state.active:
            return
        if conn.get_execution_options().get("isolation_level") != "AUTOCOMMIT":
            return
        if state.previous is None:
            return

        try:
            conn.exec_driver_sql(
                "SELECT set_config('lock_timeout', %s, false)",
                (state.previous[0],),
            )
            conn.exec_driver_sql(
                "SELECT set_config('statement_timeout', %s, false)",
                (state.previous[1],),
            )
        except Exception as exc:
            state.cleanup_error = exc
        else:
            state.active = False
            state.previous = None
            state.cleanup_error = None
        finally:
            # Alembic starts a new normal transaction after leaving the block.
            state.reapply_local = True

    event.listen(connection, "begin", on_begin)
    event.listen(connection, "commit", restore_autocommit_settings)
    event.listen(connection, "rollback", restore_autocommit_settings)

    try:
        yield
    except BaseException as exc:
        state.migration_error = exc
        raise
    finally:
        try:
            if state.active:
                if connection.invalidated:
                    # The PostgreSQL session is gone, so its session settings are gone too.
                    state.active = False
                    state.previous = None
                else:
                    if connection.in_transaction():
                        connection.rollback()

                    if state.active and state.previous is not None:
                        previous = state.previous
                        # Prevent the commit/rollback listener from recursively restoring.
                        state.active = False
                        state.reapply_local = False
                        connection.exec_driver_sql(
                            "SELECT set_config('lock_timeout', %s, false)",
                            (previous[0],),
                        )
                        connection.exec_driver_sql(
                            "SELECT set_config('statement_timeout', %s, false)",
                            (previous[1],),
                        )
                        if connection.in_transaction():
                            connection.commit()
                        state.previous = None
                        state.cleanup_error = None
        except Exception as exc:
            state.cleanup_error = exc
        finally:
            event.remove(connection, "begin", on_begin)
            event.remove(connection, "commit", restore_autocommit_settings)
            event.remove(connection, "rollback", restore_autocommit_settings)

        if state.cleanup_error is not None:
            if state.migration_error is None:
                raise state.cleanup_error
            logger.error(
                "Failed to restore PostgreSQL migration timeout settings after a migration "
                "error; the enclosing NullPool connection will be closed.",
                exc_info=(
                    type(state.cleanup_error),
                    state.cleanup_error,
                    state.cleanup_error.__traceback__,
                ),
            )
