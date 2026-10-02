"""Allow dynamic clinic laser devices across commerce and appointments.

Revision ID: 0089_dynamic_laser_device_references
Revises: 0088_service_device_registry_link
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0089_dynamic_laser_device_references"
down_revision: str | Sequence[str] | None = "0088_service_device_registry_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEVICE_COLUMNS = {
    "appointments": "laser_device_key",
    "appointment_additional_services": "laser_device_key",
    "patient_packages": "laser_device_key",
    "service_package_offers": "device_key",
    "pulse_billing_settings": "device_key",
    "pulse_pack_offers": "device_key",
    "patient_pulse_packs": "device_key",
}

_FOREIGN_KEYS = {
    "appointments": "fk_appointments_laser_device",
    "appointment_additional_services": "fk_appointment_additional_services_laser_device",
    "patient_packages": "fk_patient_packages_laser_device",
    "service_package_offers": "fk_service_package_offers_clinic_device",
    "pulse_billing_settings": "fk_pulse_billing_settings_clinic_device",
    "pulse_pack_offers": "fk_pulse_pack_offers_clinic_device",
    "patient_pulse_packs": "fk_patient_pulse_packs_clinic_device",
}

_LEGACY_CHECKS = {
    "appointments": "appointment_laser_device_valid",
    "appointment_additional_services": "appointment_additional_service_device_valid",
    "patient_packages": "patient_package_laser_device_valid",
    "service_package_offers": "service_package_offer_device_valid",
    "pulse_billing_settings": "pulse_billing_settings_device_valid",
    "pulse_pack_offers": "pulse_pack_offer_device_valid",
    "patient_pulse_packs": "patient_pulse_pack_device_valid",
}


def _drop_legacy_device_check(table: str) -> None:
    op.execute(
        sa.text(
            f"""
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
                   AND t.relname = '{table}'
                   AND c.contype = 'c'
                   AND pg_get_constraintdef(c.oid)
                       ILIKE '%prime_lase%candela_gentle%'
                 ORDER BY c.oid
                 LIMIT 1;

                IF check_name IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE public.%I DROP CONSTRAINT %I',
                        '{table}',
                        check_name
                    );
                END IF;
            END
            $$;
            """
        )
    )


def upgrade() -> None:
    for table in _DEVICE_COLUMNS:
        _drop_legacy_device_check(table)

    for table, column in _DEVICE_COLUMNS.items():
        op.create_foreign_key(
            _FOREIGN_KEYS[table],
            table,
            "clinic_laser_devices",
            ["workspace_id", column],
            ["workspace_id", "device_key"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in _DEVICE_COLUMNS.items():
        unsupported = bind.execute(
            sa.text(
                f"""
                SELECT 1
                  FROM {table}
                 WHERE {column} IS NOT NULL
                   AND {column} NOT IN ('prime_lase', 'candela_gentle')
                 LIMIT 1
                """
            )
        ).first()
        if unsupported is not None:
            raise RuntimeError(
                f"Cannot downgrade while dynamic device references exist in {table}."
            )

    for table in reversed(list(_DEVICE_COLUMNS)):
        op.drop_constraint(
            _FOREIGN_KEYS[table],
            table,
            type_="foreignkey",
        )

    for table, column in _DEVICE_COLUMNS.items():
        op.create_check_constraint(
            _LEGACY_CHECKS[table],
            table,
            f"{column} IS NULL OR {column} IN ('prime_lase', 'candela_gentle')",
        )
