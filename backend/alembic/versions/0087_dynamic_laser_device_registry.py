"""Add workspace-owned laser device registry.

Revision ID: 0087_dynamic_laser_device_registry
Revises: 0086_all_service_packages
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0087_dynamic_laser_device_registry"
down_revision: str | Sequence[str] | None = "0086_all_service_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stable_uuid_sql(suffix: str) -> str:
    digest = f"md5(w.id::text || '|{suffix}')"
    return (
        f"(substr({digest},1,8) || '-' || substr({digest},9,4) || '-' || "
        f"substr({digest},13,4) || '-' || substr({digest},17,4) || '-' || "
        f"substr({digest},21,12))::uuid"
    )


def upgrade() -> None:
    op.create_table(
        "clinic_laser_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("device_key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_clinic_laser_devices_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_clinic_laser_devices_workspace_id_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "device_key",
            name="uq_clinic_laser_devices_workspace_device_key",
        ),
    )
    op.create_index(
        "ix_clinic_laser_devices_workspace_id",
        "clinic_laser_devices",
        ["workspace_id"],
    )

    prime_id = _stable_uuid_sql("prime_lase")
    candela_id = _stable_uuid_sql("candela_gentle")
    op.execute(
        sa.text(
            f"""
            INSERT INTO clinic_laser_devices
                (id, workspace_id, device_key, name, is_active)
            SELECT {prime_id}, w.id, 'prime_lase', 'Prime Lase', true
              FROM workspaces w
            UNION ALL
            SELECT {candela_id}, w.id, 'candela_gentle', 'Candela Gentle', true
              FROM workspaces w
            ON CONFLICT (workspace_id, device_key) DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            'ALTER TABLE public."clinic_laser_devices" ENABLE ROW LEVEL SECURITY'
        )
    )
    op.execute(
        sa.text(
            'REVOKE ALL ON TABLE public."clinic_laser_devices" FROM anon, authenticated'
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_clinic_laser_devices_workspace_id",
        table_name="clinic_laser_devices",
    )
    op.drop_table("clinic_laser_devices")
