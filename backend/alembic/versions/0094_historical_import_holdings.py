"""Allow unified historical holding rows and canonical pulse-pack links.

Revision ID: 0094_history_holdings
Revises: 0093_availability_resources
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0094_history_holdings"
down_revision: str | Sequence[str] | None = "0093_availability_resources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "clinic_historical_import_row_entity_type_valid",
        "clinic_historical_import_rows",
        type_="check",
    )
    op.create_check_constraint(
        "clinic_historical_import_row_entity_type_valid",
        "clinic_historical_import_rows",
        "entity_type IN ('patient', 'appointment', 'payment', 'payment_allocation', 'package', 'holding')",
    )
    op.drop_constraint(
        "clinic_historical_import_link_entity_type_valid",
        "clinic_historical_import_links",
        type_="check",
    )
    op.create_check_constraint(
        "clinic_historical_import_link_entity_type_valid",
        "clinic_historical_import_links",
        "entity_type IN ('patient', 'appointment', 'payment', 'package', 'pulse_pack')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "clinic_historical_import_link_entity_type_valid",
        "clinic_historical_import_links",
        type_="check",
    )
    op.create_check_constraint(
        "clinic_historical_import_link_entity_type_valid",
        "clinic_historical_import_links",
        "entity_type IN ('patient', 'appointment', 'payment', 'package')",
    )
    op.drop_constraint(
        "clinic_historical_import_row_entity_type_valid",
        "clinic_historical_import_rows",
        type_="check",
    )
    op.create_check_constraint(
        "clinic_historical_import_row_entity_type_valid",
        "clinic_historical_import_rows",
        "entity_type IN ('patient', 'appointment', 'payment', 'payment_allocation', 'package')",
    )
