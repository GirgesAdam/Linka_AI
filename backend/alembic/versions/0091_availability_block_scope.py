"""Add service-scoped availability blocks.

Revision ID: 0091_availability_block_scope
Revises: 0090_availability_blocks
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0091_availability_block_scope"
down_revision: str | Sequence[str] | None = "0090_availability_blocks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "availability_blocks",
        sa.Column("scope", sa.String(length=32), server_default="all_services", nullable=False),
    )
    op.create_check_constraint(
        "availability_blocks_scope_valid",
        "availability_blocks",
        "scope IN ('all_services', 'selected_services')",
    )
    op.create_unique_constraint(
        "uq_availability_blocks_workspace_id_id",
        "availability_blocks",
        ["workspace_id", "id"],
    )
    op.create_table(
        "availability_block_services",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("availability_block_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "availability_block_id"],
            ["availability_blocks.workspace_id", "availability_blocks.id"],
            name="fk_availability_block_services_block",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "service_id"],
            ["services.workspace_id", "services.id"],
            name="fk_availability_block_services_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "availability_block_id", "service_id", name="pk_availability_block_services"
        ),
    )
    op.create_index(
        "ix_availability_block_services_workspace_service",
        "availability_block_services",
        ["workspace_id", "service_id"],
    )
    op.execute(
        sa.text('ALTER TABLE public."availability_block_services" ENABLE ROW LEVEL SECURITY')
    )
    op.execute(
        sa.text('REVOKE ALL ON TABLE public."availability_block_services" FROM anon, authenticated')
    )


def downgrade() -> None:
    op.drop_table("availability_block_services")
    op.drop_constraint(
        "uq_availability_blocks_workspace_id_id", "availability_blocks", type_="unique"
    )
    op.drop_constraint("availability_blocks_scope_valid", "availability_blocks", type_="check")
    op.drop_column("availability_blocks", "scope")
