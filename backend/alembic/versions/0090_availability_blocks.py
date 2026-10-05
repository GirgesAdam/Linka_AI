"""Add branch-wide appointment availability blocks.

Revision ID: 0090_availability_blocks
Revises: 0089_dynamic_laser_device_references
Create Date: 2026-10-05
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0090_availability_blocks"
down_revision: str | Sequence[str] | None = "0089_dynamic_laser_device_references"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "availability_blocks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("end_at > start_at", name="availability_blocks_interval_valid"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "branch_id"],
            ["branches.workspace_id", "branches.id"],
            name="fk_availability_blocks_branch",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_availability_blocks_workspace_id", "availability_blocks", ["workspace_id"])
    op.create_index("ix_availability_blocks_branch_id", "availability_blocks", ["branch_id"])
    op.create_index("ix_availability_blocks_created_by_user_id", "availability_blocks", ["created_by_user_id"])
    op.create_index(
        "ix_availability_blocks_workspace_branch_time",
        "availability_blocks",
        ["workspace_id", "branch_id", "start_at", "end_at"],
    )
    op.execute(sa.text('ALTER TABLE public."availability_blocks" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('REVOKE ALL ON TABLE public."availability_blocks" FROM anon, authenticated'))


def downgrade() -> None:
    op.drop_table("availability_blocks")
