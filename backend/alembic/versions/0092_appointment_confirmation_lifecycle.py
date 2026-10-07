"""Reconcile future appointments to the calendar-day confirmation lifecycle.

Revision ID: 0092_appt_confirmation
Revises: 0091_availability_block_scope
Create Date: 2026-10-07

Only future pending/confirmed rows are touched. The effective timezone is the
appointment branch timezone with workspace timezone fallback. Historical and
terminal appointment states are deliberately preserved.

Downgrade is intentionally a no-op: the old pending/confirmed meaning cannot be
reconstructed safely after reconciliation without inventing historical intent.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0092_appt_confirmation"
down_revision: str | Sequence[str] | None = "0091_availability_block_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH future AS (
            SELECT
                a.id,
                a.status,
                a.created_at,
                a.confirmed_at,
                a.start_at,
                COALESCE(branch_tz.name, workspace_tz.name, 'UTC') AS timezone_name
            FROM appointments AS a
            JOIN branches AS b
              ON b.id = a.branch_id AND b.workspace_id = a.workspace_id
            JOIN workspaces AS w ON w.id = a.workspace_id
            LEFT JOIN pg_timezone_names AS branch_tz
              ON branch_tz.name = NULLIF(b.timezone, '')
            LEFT JOIN pg_timezone_names AS workspace_tz
              ON workspace_tz.name = NULLIF(w.timezone, '')
            WHERE a.start_at > now()
              AND a.status IN ('pending', 'confirmed')
        )
        UPDATE appointments AS a
        SET status = 'pending',
            confirmed_at = NULL,
            updated_at = now()
        FROM future AS f
        WHERE a.id = f.id
          AND f.status = 'confirmed'
          AND (timezone(f.timezone_name, f.created_at))::date
                < (timezone(f.timezone_name, f.start_at))::date - 1
          AND (
              f.confirmed_at IS NULL
              OR (timezone(f.timezone_name, f.confirmed_at))::date
                   < (timezone(f.timezone_name, f.start_at))::date - 1
          )
        """
    )
    op.execute(
        """
        WITH future AS (
            SELECT
                a.id,
                a.status,
                a.created_at,
                a.start_at,
                COALESCE(branch_tz.name, workspace_tz.name, 'UTC') AS timezone_name
            FROM appointments AS a
            JOIN branches AS b
              ON b.id = a.branch_id AND b.workspace_id = a.workspace_id
            JOIN workspaces AS w ON w.id = a.workspace_id
            LEFT JOIN pg_timezone_names AS branch_tz
              ON branch_tz.name = NULLIF(b.timezone, '')
            LEFT JOIN pg_timezone_names AS workspace_tz
              ON workspace_tz.name = NULLIF(w.timezone, '')
            WHERE a.start_at > now()
              AND a.status = 'pending'
        )
        UPDATE appointments AS a
        SET status = 'confirmed',
            confirmed_at = f.created_at,
            updated_at = now()
        FROM future AS f
        WHERE a.id = f.id
          AND (timezone(f.timezone_name, f.created_at))::date
                >= (timezone(f.timezone_name, f.start_at))::date - 1
          AND (timezone(f.timezone_name, f.created_at))::date
                <= (timezone(f.timezone_name, f.start_at))::date
        """
    )


def downgrade() -> None:
    # Reconciliation is intentionally irreversible; old semantic intent is not
    # recoverable safely from the post-migration rows.
    pass
