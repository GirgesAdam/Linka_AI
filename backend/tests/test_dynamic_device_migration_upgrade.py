from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.branch import Branch
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.patient_package import PatientPackage
from app.models.pulse_billing import (
    PatientPulsePack,
    PulseBillingSettings,
    PulsePackOffer,
)
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.services.inventory import (
    create_clinic_laser_device,
    list_clinic_laser_devices,
    upsert_laser_device_price,
)

BACKEND = Path(__file__).resolve().parent.parent
MIGRATION_FKS = {
    "fk_service_device_prices_clinic_device",
    "fk_appointments_laser_device",
    "fk_appointment_additional_services_laser_device",
    "fk_patient_packages_laser_device",
    "fk_service_package_offers_clinic_device",
    "fk_pulse_billing_settings_clinic_device",
    "fk_pulse_pack_offers_clinic_device",
    "fk_patient_pulse_packs_clinic_device",
}


def _upgrade(url: str, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(config, revision)


@pytest.fixture
def migration_database(monkeypatch: pytest.MonkeyPatch):
    base = make_url(settings.database_url)
    database_name = f"tia_dynamic_upgrade_{uuid4().hex[:12]}"
    admin_url = base.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    except OperationalError:
        admin_engine.dispose()
        pytest.skip("PostgreSQL is not available for populated migration integration test.")

    target_url = base.set(database=database_name)
    try:
        yield str(target_url)
    finally:
        admin_engine.dispose()
        cleanup_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with cleanup_engine.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
        finally:
            cleanup_engine.dispose()


def _seed_legacy_rows(url: str) -> dict[str, object]:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            suffix = uuid4().hex[:10]
            workspace = Workspace(
                name=f"Legacy Device Clinic {suffix}",
                slug=f"legacy-device-{suffix}",
                timezone="Africa/Cairo",
                is_active=True,
                is_demo=False,
            )
            db.add(workspace)
            db.flush()

            branch = Branch(
                workspace_id=workspace.id,
                name="Main",
                code="MAIN",
                phone=None,
                email=None,
                address_line1=None,
                address_line2=None,
                city="Cairo",
                state=None,
                country_code="EG",
                timezone="Africa/Cairo",
                is_active=True,
            )
            db.add(branch)
            db.flush()
            workspace.primary_branch_id = branch.id

            service = Service(
                workspace_id=workspace.id,
                name="Legacy Laser",
                slug=f"legacy-laser-{suffix}",
                category="Laser",
                operational_category="laser",
                description=None,
                duration_minutes=60,
                buffer_before_minutes=0,
                buffer_after_minutes=0,
                price_minor=100_000,
                currency="EGP",
                requires_medical_review=False,
                requires_laser_device=True,
                is_active=True,
            )
            staff = Staff(
                workspace_id=workspace.id,
                user_id=None,
                first_name="Legacy",
                last_name="Doctor",
                email=None,
                phone=None,
                job_title="doctor",
                is_active=True,
            )
            patient = Patient(
                workspace_id=workspace.id,
                first_name="Legacy",
                last_name="Patient",
                phone="01044444444",
                phone_normalized="+201044444444",
                gender="female",
                preferred_language="ar",
                preferred_branch_id=branch.id,
                source="referral",
                status="active",
                marketing_consent=False,
            )
            db.add_all([service, staff, patient])
            db.flush()

            doctor = Doctor(
                workspace_id=workspace.id,
                staff_id=staff.id,
                doctor_type="regular",
                specialization=None,
                license_number=None,
                bio=None,
                booking_enabled=True,
                is_active=True,
            )
            db.add(doctor)
            db.flush()

            prime_price = ServiceDevicePrice(
                workspace_id=workspace.id,
                service_id=service.id,
                device_key="prime_lase",
                device_name="Prime Lase",
                price_minor=110_000,
                duration_minutes=55,
                currency="EGP",
                is_active=True,
            )
            candela_price = ServiceDevicePrice(
                workspace_id=workspace.id,
                service_id=service.id,
                device_key="candela_gentle",
                device_name="Candela Gentle",
                price_minor=130_000,
                duration_minutes=60,
                currency="EGP",
                is_active=True,
            )
            service_offer = ServicePackageOffer(
                workspace_id=workspace.id,
                service_id=service.id,
                device_key="candela_gentle",
                device_name="Candela Gentle",
                sessions_count=6,
                price_minor=700_000,
                currency="EGP",
                is_active=True,
            )
            pulse_settings = PulseBillingSettings(
                workspace_id=workspace.id,
                device_key="prime_lase",
                overage_price_minor=150,
                currency="EGP",
            )
            pulse_offer = PulsePackOffer(
                workspace_id=workspace.id,
                device_key="candela_gentle",
                device_name="Candela Gentle",
                pulses_count=1000,
                price_minor=120_000,
                currency="EGP",
                is_active=True,
            )
            db.add_all(
                [
                    prime_price,
                    candela_price,
                    service_offer,
                    pulse_settings,
                    pulse_offer,
                ]
            )
            db.flush()

            package = PatientPackage(
                workspace_id=workspace.id,
                patient_id=patient.id,
                service_id=service.id,
                purchase_transaction_id=None,
                package_offer_id=None,
                origin_appointment_id=None,
                created_by_user_id=None,
                external_id=None,
                name="Legacy Prime Package",
                sessions_purchased=6,
                opening_sessions_remaining=3,
                sessions_total_known=True,
                sale_price_minor=600_000,
                standalone_session_price_minor_at_purchase=110_000,
                laser_device_key="prime_lase",
                laser_device_name="Prime Lase",
                currency="EGP",
                purchased_at=datetime(2026, 1, 2, tzinfo=UTC),
                expires_at=None,
                status="active",
                source="staff",
                idempotency_key=None,
            )
            pulse_pack = PatientPulsePack(
                workspace_id=workspace.id,
                patient_id=patient.id,
                pulse_pack_offer_id=pulse_offer.id,
                origin_appointment_id=None,
                purchase_transaction_id=None,
                created_by_user_id=None,
                device_key="candela_gentle",
                device_name="Candela Gentle",
                pulses_purchased=1000,
                sale_price_minor=120_000,
                standalone_pulse_price_minor_at_purchase=150,
                currency="EGP",
                purchased_at=datetime(2026, 1, 2, tzinfo=UTC),
                expires_at=None,
                status="active",
                idempotency_key=None,
            )
            db.add_all([package, pulse_pack])
            db.flush()

            start_at = datetime(2026, 1, 3, 10, 0, tzinfo=UTC)
            appointment = Appointment(
                workspace_id=workspace.id,
                patient_id=patient.id,
                branch_id=branch.id,
                doctor_id=doctor.id,
                doctor_assignment_known=True,
                is_quick_booking=False,
                service_id=service.id,
                patient_package_id=None,
                visit_group_id=None,
                lead_id=None,
                created_by_user_id=None,
                rescheduled_from_appointment_id=None,
                status="completed",
                source="staff",
                start_at=start_at,
                end_at=start_at + timedelta(minutes=55),
                busy_start_at=start_at,
                busy_end_at=start_at + timedelta(minutes=55),
                duration_minutes=55,
                price_minor=110_000,
                discount_minor=0,
                currency="EGP",
                laser_device_key="prime_lase",
                laser_device_name="Prime Lase",
                laser_pulses_used=400,
                payment_status="paid",
                amount_paid_minor=110_000,
                payment_method="cash",
                billing_context="standard",
                package_external_id=None,
                customer_note=None,
                cancellation_reason=None,
                idempotency_key=None,
            )
            db.add(appointment)
            db.flush()

            additional = AppointmentAdditionalService(
                workspace_id=workspace.id,
                appointment_id=appointment.id,
                service_id=service.id,
                patient_package_id=None,
                service_name=service.name,
                unit_price_minor=130_000,
                currency="EGP",
                laser_device_key="candela_gentle",
                laser_device_name="Candela Gentle",
                billing_context="standard",
                laser_pulses_used=300,
                pulse_resolution=None,
                pulse_resolution_pulse_pack_id=None,
                pulse_overage_unit_price_minor=None,
                pulse_overage_charge_minor=0,
                created_by_user_id=None,
            )
            db.add(additional)
            db.commit()
            return {
                "workspace_id": workspace.id,
                "service_id": service.id,
                "prime_price_id": prime_price.id,
                "candela_price_id": candela_price.id,
                "appointment_id": appointment.id,
                "additional_id": additional.id,
                "package_id": package.id,
                "service_offer_id": service_offer.id,
                "pulse_settings_id": pulse_settings.id,
                "pulse_offer_id": pulse_offer.id,
                "pulse_pack_id": pulse_pack.id,
            }
    finally:
        engine.dispose()


def test_populated_0086_to_0089_dynamic_device_upgrade(
    migration_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(migration_database, "0086_all_service_packages", monkeypatch)
    ids = _seed_legacy_rows(migration_database)

    _upgrade(
        migration_database,
        "0089_dynamic_laser_device_references",
        monkeypatch,
    )

    engine = create_engine(migration_database, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            registry = connection.execute(
                text(
                    """
                    SELECT device_key, name
                      FROM clinic_laser_devices
                     WHERE workspace_id = :workspace_id
                     ORDER BY device_key
                    """
                ),
                {"workspace_id": ids["workspace_id"]},
            ).all()
            assert registry == [
                ("candela_gentle", "Candela Gentle"),
                ("prime_lase", "Prime Lase"),
            ]

            preservation_checks = [
                (
                    "service_device_prices",
                    "id",
                    ids["prime_price_id"],
                    "device_key",
                    "device_name",
                    "prime_lase",
                    "Prime Lase",
                ),
                (
                    "service_device_prices",
                    "id",
                    ids["candela_price_id"],
                    "device_key",
                    "device_name",
                    "candela_gentle",
                    "Candela Gentle",
                ),
                (
                    "appointments",
                    "id",
                    ids["appointment_id"],
                    "laser_device_key",
                    "laser_device_name",
                    "prime_lase",
                    "Prime Lase",
                ),
                (
                    "appointment_additional_services",
                    "id",
                    ids["additional_id"],
                    "laser_device_key",
                    "laser_device_name",
                    "candela_gentle",
                    "Candela Gentle",
                ),
                (
                    "patient_packages",
                    "id",
                    ids["package_id"],
                    "laser_device_key",
                    "laser_device_name",
                    "prime_lase",
                    "Prime Lase",
                ),
                (
                    "service_package_offers",
                    "id",
                    ids["service_offer_id"],
                    "device_key",
                    "device_name",
                    "candela_gentle",
                    "Candela Gentle",
                ),
                (
                    "pulse_pack_offers",
                    "id",
                    ids["pulse_offer_id"],
                    "device_key",
                    "device_name",
                    "candela_gentle",
                    "Candela Gentle",
                ),
                (
                    "patient_pulse_packs",
                    "id",
                    ids["pulse_pack_id"],
                    "device_key",
                    "device_name",
                    "candela_gentle",
                    "Candela Gentle",
                ),
            ]
            for (
                table,
                id_column,
                row_id,
                key_column,
                name_column,
                expected_key,
                expected_name,
            ) in preservation_checks:
                actual = connection.execute(
                    text(
                        f"SELECT {key_column}, {name_column} "
                        f"FROM {table} WHERE {id_column} = :row_id"
                    ),
                    {"row_id": row_id},
                ).one()
                assert actual == (expected_key, expected_name)

            pulse_settings = connection.execute(
                text(
                    """
                    SELECT device_key, overage_price_minor
                      FROM pulse_billing_settings
                     WHERE id = :row_id
                    """
                ),
                {"row_id": ids["pulse_settings_id"]},
            ).one()
            assert pulse_settings == ("prime_lase", 150)

            fk_stmt = (
                text(
                    """
                    SELECT conname, convalidated
                      FROM pg_constraint
                     WHERE conname IN :names
                    """
                )
                .bindparams(bindparam("names", expanding=True))
            )
            foreign_keys = dict(
                connection.execute(
                    fk_stmt,
                    {"names": sorted(MIGRATION_FKS)},
                ).all()
            )
            assert set(foreign_keys) == MIGRATION_FKS
            assert all(foreign_keys.values())

        with Session(engine) as db:
            arbitrary = create_clinic_laser_device(
                db,
                workspace_id=ids["workspace_id"],
                name="DEKA Again",
            )
            assert arbitrary.device_key.startswith("device_")
            assert arbitrary.name == "DEKA Again"
            price = upsert_laser_device_price(
                db,
                workspace_id=ids["workspace_id"],
                service_id=ids["service_id"],
                device_key=arbitrary.device_key,
                price_minor=155_000,
                duration_minutes=50,
                currency="EGP",
            )
            assert price.device_key == arbitrary.device_key
            assert price.device_name == "DEKA Again"

            suffix = uuid4().hex[:10]
            new_workspace = Workspace(
                name=f"Post Migration Clinic {suffix}",
                slug=f"post-migration-{suffix}",
                timezone="Africa/Cairo",
                is_active=True,
                is_demo=False,
            )
            db.add(new_workspace)
            db.flush()
            assert list_clinic_laser_devices(
                db,
                workspace_id=new_workspace.id,
            ) == []
            db.commit()
    finally:
        engine.dispose()
