"""Add first-class schedule-resource scopes to availability blocks.

Revision ID: 0093_availability_resources
Revises: 0092_appt_confirmation
Create Date: 2026-10-07

Legacy selected_services rows are deliberately left untouched. Their semantics are
not equivalent to schedule resources, especially for laser services that can run
on multiple devices.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0093_availability_resources"
down_revision: str | Sequence[str] | None = "0092_appt_confirmation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("availability_blocks_scope_valid", "availability_blocks", type_="check")
    op.create_check_constraint(
        "availability_blocks_scope_valid",
        "availability_blocks",
        "scope IN ('all_services', 'selected_services', 'selected_resources')",
    )
    op.create_table(
        "availability_block_targets",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("availability_block_id", sa.Uuid(), nullable=False),
        sa.Column("target_key", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "availability_block_id"],
            ["availability_blocks.workspace_id", "availability_blocks.id"],
            ondelete="CASCADE",
            name="fk_availability_block_targets_block",
        ),
        sa.PrimaryKeyConstraint(
            "availability_block_id", "target_key", name="pk_availability_block_targets"
        ),
    )
    op.create_index(
        "ix_availability_block_targets_workspace_target",
        "availability_block_targets",
        ["workspace_id", "target_key"],
    )
    op.execute(
        sa.text('ALTER TABLE public."availability_block_targets" ENABLE ROW LEVEL SECURITY')
    )
    op.execute(
        sa.text('REVOKE ALL ON TABLE public."availability_block_targets" FROM anon, authenticated')
    )


def downgrade() -> None:
    op.drop_index(
        "ix_availability_block_targets_workspace_target",
        table_name="availability_block_targets",
    )
    op.drop_table("availability_block_targets")
    op.execute("DELETE FROM availability_blocks WHERE scope = 'selected_resources'")
    op.drop_constraint("availability_blocks_scope_valid", "availability_blocks", type_="check")
    op.create_check_constraint(
        "availability_blocks_scope_valid",
        "availability_blocks",
        "scope IN ('all_services', 'selected_services')",
    )
