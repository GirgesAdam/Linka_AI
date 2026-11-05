"""Add optional page permissions and version to workspace memberships.

Revision ID: 0097_member_page_permissions
Revises: 0096_patient_customer_id
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0097_member_page_permissions"
down_revision: str | Sequence[str] | None = "0096_patient_customer_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Leave existing memberships NULL: no automatic permission assignment.
    op.add_column(
        "workspace_members",
        sa.Column("page_permissions", JSONB(), nullable=True),
    )
    op.add_column(
        "workspace_members",
        sa.Column("permissions_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("workspace_members", "permissions_version")
    op.drop_column("workspace_members", "page_permissions")
