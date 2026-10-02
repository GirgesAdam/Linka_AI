"""Link service device pricing to workspace device registry.

Revision ID: 0088_service_device_registry_link
Revises: 0087_dynamic_laser_device_registry
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0088_service_device_registry_link"
down_revision: str | Sequence[str] | None = "0087_dynamic_laser_device_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_legacy_device_check() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                check_name text;
            BEGIN
                SELECT c.conname
                  INTO check_name
                  FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                  JOIN pg_namespace n ON n.oid = t.relnamespace
                 WHERE n.nspname = 'public'
                   AND t.relname = 'service_device_prices'
                   AND c.contype = 'c'
                   AND pg_get_constraintdef(c.oid)
                       ILIKE '%device_key%prime_lase%candela_gentle%'
                 ORDER BY c.oid
                 LIMIT 1;

                IF check_name IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE public.%I DROP CONSTRAINT %I',
                        'service_device_prices',
                        check_name
                    );
                END IF;
            END
            $$;
            """
        )
    )


def upgrade() -> None:
    _drop_legacy_device_check()
    op.create_foreign_key(
        "fk_service_device_prices_clinic_device",
        "service_device_prices",
        "clinic_laser_devices",
        ["workspace_id", "device_key"],
        ["workspace_id", "device_key"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    unsupported = bind.execute(
        sa.text(
            """
            SELECT 1
              FROM service_device_prices
             WHERE device_key NOT IN ('prime_lase', 'candela_gentle')
             LIMIT 1
            """
        )
    ).first()
    if unsupported is not None:
        raise RuntimeError(
            "Cannot downgrade while dynamic laser device pricing rows exist."
        )

    op.drop_constraint(
        "fk_service_device_prices_clinic_device",
        "service_device_prices",
        type_="foreignkey",
    )
    op.create_check_constraint(
        "service_device_price_device_valid",
        "service_device_prices",
        "device_key IN ('prime_lase', 'candela_gentle')",
    )
