"""Normalize laser service requirement from operational category.

Revision ID: 0095_service_laser_category
Revises: 0094_history_holdings
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0095_service_laser_category"
down_revision: str | Sequence[str] | None = "0094_history_holdings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE services "
        "SET requires_laser_device = (operational_category = 'laser') "
        "WHERE requires_laser_device IS DISTINCT FROM (operational_category = 'laser')"
    )
    op.execute(
        "UPDATE service_device_prices AS prices "
        "SET is_active = false "
        "FROM services AS services "
        "WHERE prices.workspace_id = services.workspace_id "
        "AND prices.service_id = services.id "
        "AND services.operational_category <> 'laser' "
        "AND prices.is_active = true"
    )
    op.create_check_constraint(
        "service_laser_requirement_matches_category",
        "services",
        "requires_laser_device = (operational_category = 'laser')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "service_laser_requirement_matches_category",
        "services",
        type_="check",
    )
