from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.booking as booking_service
from app.agents.v2.semantic_context import build_semantic_context
from app.core.config import settings
from app.integrations.clinic.tia_database_laser import TiaDatabaseLaserClinicAdapter
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.patient_package import PatientPackage
from app.models.pulse_billing import (
    AppointmentPulseSettlement,
    PatientPulsePack,
    PulseBillingSettings,
    PulsePackOffer,
    PulseUsage,
)
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.models.staff import Staff
from app.models.workspace import Workspace
from app.schemas.crm import normalize_patient_identity_phone
from app.services import inventory as inventory_service
from app.services import package_offers as package_offer_service
from app.services import patient_packages as package_service
from app.services import pulse_billing as pulse_service
from app.services.inventory import InventoryOperationError
from app.services.package_offers import PackageOfferError
from app.services.pulse_billing import PulseBillingError


@contextmanager
def _db_session():
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield db
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
        engine.dispose()


def _seed_inactive_device_lifecycle(db: Session):
    suffix = uuid4().hex[:10]
    workspace = Workspace(
        name=f"Inactive Device {suffix}",
        slug=f"inactive-device-{suffix}",
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
        name="Full Body Laser",
        slug=f"full-body-laser-{suffix}",
        category="Laser",
        operational_category="laser",
        description=None,
        duration_minutes=60,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=155_000,
        currency="EGP",
        requires_medical_review=False,
        requires_laser_device=True,
        is_active=True,
    )
    staff = Staff(
        workspace_id=workspace.id,
        user_id=None,
        first_name="Dr.",
        last_name="Lifecycle",
        email=None,
        phone=None,
        job_title="doctor",
        is_active=True,
    )
    db.add_all([service, staff])
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

    display, normalized = normalize_patient_identity_phone("01055555555")
    patient = Patient(
        workspace_id=workspace.id,
        first_name="Existing",
        last_name="Patient",
        phone=display,
        phone_normalized=normalized,
        gender="female",
        preferred_language="ar",
        preferred_branch_id=branch.id,
        source="referral",
        status="active",
        marketing_consent=False,
    )
    device = ClinicLaserDevice(
        workspace_id=workspace.id,
        device_key="device_deka_again",
        name="DEKA Again",
        is_active=True,
    )
    db.add_all([patient, device])
    db.flush()

    device_price = ServiceDevicePrice(
        workspace_id=workspace.id,
        service_id=service.id,
        device_key=device.device_key,
        device_name=device.name,
        price_minor=155_000,
        duration_minutes=50,
        currency="EGP",
        is_active=True,
    )
    package_offer = ServicePackageOffer(
        workspace_id=workspace.id,
        service_id=service.id,
        device_key=device.device_key,
        device_name=device.name,
        sessions_count=6,
        price_minor=800_000,
        currency="EGP",
        is_active=True,
    )
    pulse_settings = PulseBillingSettings(
        workspace_id=workspace.id,
        device_key=device.device_key,
        overage_price_minor=150,
        currency="EGP",
    )
    pulse_offer = PulsePackOffer(
        workspace_id=workspace.id,
        device_key=device.device_key,
        device_name=device.name,
        pulses_count=1000,
        price_minor=120_000,
        currency="EGP",
        is_active=True,
    )
    db.add_all([device_price, package_offer, pulse_settings, pulse_offer])
    db.flush()

    purchased_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    historical_package = PatientPackage(
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service.id,
        purchase_transaction_id=None,
        package_offer_id=package_offer.id,
        origin_appointment_id=None,
        created_by_user_id=None,
        external_id=None,
        name="Legacy DEKA package",
        sessions_purchased=6,
        opening_sessions_remaining=2,
        sessions_total_known=True,
        sale_price_minor=800_000,
        standalone_session_price_minor_at_purchase=None,
        laser_device_key=device.device_key,
        laser_device_name=device.name,
        currency="EGP",
        purchased_at=purchased_at,
        expires_at=None,
        status="active",
        source="staff",
        idempotency_key=None,
    )
    first_pulse_pack = PatientPulsePack(
        workspace_id=workspace.id,
        patient_id=patient.id,
        pulse_pack_offer_id=pulse_offer.id,
        origin_appointment_id=None,
        purchase_transaction_id=None,
        created_by_user_id=None,
        device_key=device.device_key,
        device_name=device.name,
        pulses_purchased=1000,
        sale_price_minor=120_000,
        standalone_pulse_price_minor_at_purchase=150,
        currency="EGP",
        purchased_at=purchased_at,
        expires_at=None,
        status="active",
        idempotency_key=None,
    )
    second_pulse_pack = PatientPulsePack(
        workspace_id=workspace.id,
        patient_id=patient.id,
        pulse_pack_offer_id=pulse_offer.id,
        origin_appointment_id=None,
        purchase_transaction_id=None,
        created_by_user_id=None,
        device_key=device.device_key,
        device_name=device.name,
        pulses_purchased=500,
        sale_price_minor=70_000,
        standalone_pulse_price_minor_at_purchase=150,
        currency="EGP",
        purchased_at=purchased_at + timedelta(minutes=1),
        expires_at=None,
        status="active",
        idempotency_key=None,
    )
    db.add_all([historical_package, first_pulse_pack, second_pulse_pack])
    db.flush()

    start = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
    existing_appointment = Appointment(
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
        status="confirmed",
        source="staff",
        start_at=start,
        end_at=start + timedelta(minutes=50),
        busy_start_at=start,
        busy_end_at=start + timedelta(minutes=50),
        duration_minutes=50,
        price_minor=155_000,
        discount_minor=0,
        currency="EGP",
        laser_device_key=device.device_key,
        laser_device_name=device.name,
        laser_pulses_used=200,
        payment_status="unpaid",
        amount_paid_minor=0,
        payment_method="unknown",
        billing_context="pulse_prepaid",
        package_external_id=None,
        customer_note=None,
        cancellation_reason=None,
        idempotency_key=None,
    )
    pending_start = start + timedelta(hours=2)
    pending_appointment = Appointment(
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
        status="confirmed",
        source="staff",
        start_at=pending_start,
        end_at=pending_start + timedelta(minutes=50),
        busy_start_at=pending_start,
        busy_end_at=pending_start + timedelta(minutes=50),
        duration_minutes=50,
        price_minor=155_000,
        discount_minor=0,
        currency="EGP",
        laser_device_key=device.device_key,
        laser_device_name=device.name,
        laser_pulses_used=1000,
        payment_status="unpaid",
        amount_paid_minor=0,
        payment_method="unknown",
        billing_context="pulse_prepaid",
        package_external_id=None,
        customer_note=None,
        cancellation_reason=None,
        idempotency_key=None,
    )
    db.add_all([existing_appointment, pending_appointment])
    db.flush()

    db.add_all(
        [
            PulseUsage(
                workspace_id=workspace.id,
                patient_pulse_pack_id=first_pulse_pack.id,
                appointment_id=existing_appointment.id,
                appointment_additional_service_id=None,
                pulses_used=200,
                status="consumed",
                used_at=start,
            ),
            PulseUsage(
                workspace_id=workspace.id,
                patient_pulse_pack_id=first_pulse_pack.id,
                appointment_id=pending_appointment.id,
                appointment_additional_service_id=None,
                pulses_used=800,
                status="consumed",
                used_at=pending_start,
            ),
        ]
    )
    existing_settlement = AppointmentPulseSettlement(
        workspace_id=workspace.id,
        appointment_id=existing_appointment.id,
        pulses_used=200,
        pulses_from_balance=200,
        deficit_pulses=0,
        resolution="balance",
        resolution_pulse_pack_id=None,
        overage_unit_price_minor=None,
        overage_charge_minor=0,
        resolved_at=start,
    )
    pending_settlement = AppointmentPulseSettlement(
        workspace_id=workspace.id,
        appointment_id=pending_appointment.id,
        pulses_used=1000,
        pulses_from_balance=800,
        deficit_pulses=200,
        resolution="pending",
        resolution_pulse_pack_id=None,
        overage_unit_price_minor=None,
        overage_charge_minor=0,
        resolved_at=None,
    )
    db.add_all([existing_settlement, pending_settlement])
    db.flush()

    return {
        "workspace": workspace,
        "branch": branch,
        "service": service,
        "doctor": doctor,
        "patient": patient,
        "device": device,
        "package": historical_package,
        "existing_appointment": existing_appointment,
        "existing_settlement": existing_settlement,
        "pending_appointment": pending_appointment,
        "pending_settlement": pending_settlement,
    }


def test_inactive_device_blocks_new_use_but_preserves_existing_lifecycle(monkeypatch) -> None:
    monkeypatch.setattr(pulse_service, "record_activity_event", lambda *args, **kwargs: None)

    with _db_session() as db:
        rows = _seed_inactive_device_lifecycle(db)
        workspace = rows["workspace"]
        branch = rows["branch"]
        service = rows["service"]
        patient = rows["patient"]
        device = rows["device"]
        existing_appointment = rows["existing_appointment"]
        existing_settlement = rows["existing_settlement"]
        pending_appointment = rows["pending_appointment"]
        pending_settlement = rows["pending_settlement"]

        before = pulse_service.pulse_settlement_read(
            db,
            settlement=existing_settlement,
            appointment=existing_appointment,
        )
        assert before.resolution == "balance"
        assert before.available_balance_after == 500
        assert before.currency == "EGP"

        disabled = inventory_service.update_clinic_laser_device(
            db,
            workspace_id=workspace.id,
            device_id=device.id,
            name=None,
            is_active=False,
        )
        assert disabled.device_key == device.device_key
        assert disabled.is_active is False
        db.expire_all()

        after = pulse_service.pulse_settlement_read(
            db,
            settlement=db.get(AppointmentPulseSettlement, existing_settlement.id),
            appointment=db.get(Appointment, existing_appointment.id),
        )
        assert after.resolution == "balance"
        assert after.available_balance_after == 500
        assert after.currency == "EGP"

        pending_read = pulse_service.pulse_settlement_read(
            db,
            settlement=db.get(AppointmentPulseSettlement, pending_settlement.id),
            appointment=db.get(Appointment, pending_appointment.id),
        )
        assert pending_read.resolution == "pending"
        assert pending_read.deficit_pulses == 200

        balances = pulse_service.list_patient_pulse_balances(
            db,
            workspace_id=workspace.id,
            patient_id=patient.id,
        )
        assert len(balances) == 1
        assert balances[0].device_key == device.device_key
        assert balances[0].device_name == "DEKA Again"
        assert balances[0].pulses_remaining == 500

        pulse_packs = pulse_service.list_patient_pulse_packs(
            db,
            workspace_id=workspace.id,
            patient_id=patient.id,
        )
        assert len(pulse_packs) == 2
        assert {pack.device_name for pack in pulse_packs} == {"DEKA Again"}

        legacy_package = package_service.package_read(
            db,
            db.get(PatientPackage, rows["package"].id),
        )
        assert legacy_package.laser_device_name == "DEKA Again"
        assert legacy_package.standalone_session_price_minor_at_purchase is None
        assert legacy_package.cancellation_consumed_sessions == 4
        assert legacy_package.cancellation_default_charge_minor == 620_000

        resolved = pulse_service.resolve_pulse_deficit_with_overage(
            db,
            workspace_id=workspace.id,
            appointment_id=pending_appointment.id,
            changed_by_user_id=None,
        )
        assert resolved.resolution == "overage"
        assert resolved.overage_unit_price_minor == 150
        assert resolved.overage_charge_minor == 30_000
        assert resolved.resolved_at is not None

        assert inventory_service.list_clinic_laser_devices(
            db,
            workspace_id=workspace.id,
            active_only=True,
        ) == []
        assert inventory_service.list_laser_device_prices(
            db,
            workspace_id=workspace.id,
        ) == []
        assert package_offer_service.list_package_offers(
            db,
            workspace_id=workspace.id,
            active_only=True,
        ) == []
        assert pulse_service.list_pulse_pack_offers(
            db,
            workspace_id=workspace.id,
            active_only=True,
        ) == []
        assert pulse_service.list_pulse_billing_settings(
            db,
            workspace_id=workspace.id,
        ) == []

        catalog = TiaDatabaseLaserClinicAdapter(
            db=db,
            workspace=workspace,
        ).build_catalog()
        semantic = build_semantic_context(catalog)
        assert semantic.model_input["devices"] == []

        with pytest.raises(booking_service.BookingRuleError, match="not active"):
            booking_service.calculate_availability(
                db=db,
                workspace=workspace,
                branch_id=branch.id,
                service_id=service.id,
                booking_date=date(2026, 2, 1),
                doctor_id=None,
                now=datetime(2026, 1, 15, tzinfo=UTC),
                preloaded_branch=branch,
                preloaded_service=service,
                laser_device_key=device.device_key,
            )

        with pytest.raises(InventoryOperationError, match="not active"):
            inventory_service.upsert_laser_device_price(
                db,
                workspace_id=workspace.id,
                service_id=service.id,
                device_key=device.device_key,
                price_minor=160_000,
                duration_minutes=50,
                currency="EGP",
            )

        with pytest.raises(PulseBillingError, match="not active"):
            pulse_service.upsert_pulse_billing_settings(
                db,
                workspace_id=workspace.id,
                device_key=device.device_key,
                overage_price_minor=160,
                currency="EGP",
            )

        with pytest.raises(PulseBillingError, match="not active"):
            pulse_service.upsert_pulse_pack_offer(
                db,
                workspace_id=workspace.id,
                device_key=device.device_key,
                pulses_count=2000,
                price_minor=200_000,
                currency="EGP",
                is_active=True,
            )

        with pytest.raises(PackageOfferError, match="not active"):
            package_offer_service.upsert_package_offer(
                db,
                workspace_id=workspace.id,
                service_id=service.id,
                device_key=device.device_key,
                sessions_count=8,
                price_minor=1_000_000,
                currency="EGP",
                is_active=True,
            )
