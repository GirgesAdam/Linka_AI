"""Add optional clinic-managed customer ID to patient profile.

Revision ID: 0096_patient_customer_id
Revises: 0095_service_laser_category
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0096_patient_customer_id"
down_revision: str | Sequence[str] | None = "0095_service_laser_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("customer_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("patients", "customer_id")
