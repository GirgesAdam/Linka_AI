"""Allow selected doctor/weekday/branch laser-device exclusions; default ON.

Revision ID: 0098_doctor_laser_weekday_exclusions
Revises: 0097_member_page_permissions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0098_doctor_laser_weekday_exclusions"
down_revision: str | Sequence[str] | None = "0097_member_page_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "doctor_laser_device_weekday_exclusions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("doctor_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("device_key", sa.String(length=40), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_doctor_laser_weekday_valid"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "doctor_id", "branch_id"],
            ["doctor_branches.workspace_id", "doctor_branches.doctor_id", "doctor_branches.branch_id"],
            ondelete="CASCADE", name="fk_doctor_laser_weekday_assignment",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_key"],
            ["clinic_laser_devices.workspace_id", "clinic_laser_devices.device_key"],
            ondelete="CASCADE", name="fk_doctor_laser_weekday_device",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "doctor_id", "branch_id", "weekday", "device_key",
            name="uq_doctor_laser_weekday_exclusion",
        ),
    )
    for column in ("workspace_id", "doctor_id", "branch_id"):
        op.create_index(
            f"ix_doctor_laser_device_weekday_exclusions_{column}",
            "doctor_laser_device_weekday_exclusions",
            [column],
        )

    # Workspace-owned scheduling data is backend-only, never exposed through
    # Supabase Data API/browser roles (consistent with earlier clinic tables).
    op.execute(sa.text(
        'ALTER TABLE public."doctor_laser_device_weekday_exclusions" ENABLE ROW LEVEL SECURITY'
    ))
    op.execute(sa.text(
        'REVOKE ALL ON TABLE public."doctor_laser_device_weekday_exclusions" FROM PUBLIC, anon, authenticated'
    ))


def downgrade() -> None:
    op.drop_table("doctor_laser_device_weekday_exclusions")
