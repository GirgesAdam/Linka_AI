from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import PatientPulsePack
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.services import package_offers as package_offer_service
from app.services import patient_packages as patient_package_service
from app.services import pulse_billing as pulse_billing_service


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for model in (
        Patient,
        Service,
        ServicePackageOffer,
        PaymentTransaction,
        PatientPackage,
        PackageUsage,
        PatientPulsePack,
    ):
        model.__table__.create(engine)
    return engine


def test_opening_package_balance_uses_offer_total_without_creating_payment(monkeypatch) -> None:
    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    engine = _engine()
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()

    with Session(engine) as db:
        patient = Patient(workspace_id=workspace_id, id=patient_id, first_name="Ada")
        service = Service(
            workspace_id=workspace_id,
            id=service_id,
            name="Laser",
            slug="laser",
            operational_category="dermatology",
            requires_laser_device=False,
            duration_minutes=30,
            price_minor=25_000,
        )
        offer = ServicePackageOffer(
            workspace_id=workspace_id,
            service_id=service_id,
            device_key=None,
            device_name=None,
            sessions_count=6,
            price_minor=120_000,
            currency="EGP",
            is_active=True,
        )
        db.add_all([patient, service, offer])
        db.flush()

        package = package_offer_service.create_patient_package_opening_balance(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            offer_id=offer.id,
            sessions_remaining=2,
            created_by_user_id=None,
        )

        assert package.sessions_purchased == 6
        assert package.opening_sessions_remaining == 2
        assert package.sessions_total_known is True
        assert package.sale_price_minor == 0
        assert package.purchase_transaction_id is None
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0

        with pytest.raises(package_offer_service.PackageOfferError):
            package_offer_service.create_patient_package_opening_balance(
                db,
                workspace_id=workspace_id,
                patient_id=patient_id,
                offer_id=offer.id,
                sessions_remaining=7,
                created_by_user_id=None,
            )
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 1


def test_opening_pulse_balance_uses_existing_device_without_creating_payment(monkeypatch) -> None:
    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pulse_billing_service,
        "_active_clinic_laser_device",
        lambda *args, **kwargs: SimpleNamespace(device_key="candela", name="Candela"),
    )
    engine = _engine()
    workspace_id, patient_id = uuid4(), uuid4()

    with Session(engine) as db:
        patient = Patient(workspace_id=workspace_id, id=patient_id, first_name="Ada")
        db.add(patient)
        db.flush()

        pack = pulse_billing_service.create_patient_pulse_opening_balance(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            device_key="candela",
            pulses_remaining=750,
            created_by_user_id=None,
        )

        assert pack.pulses_purchased == 750
        assert pack.sale_price_minor == 0
        assert pack.purchase_transaction_id is None
        assert pack.pulse_pack_offer_id is None
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_reception_registration_ui_and_api_keep_optional_balances_explicit() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    page = (root / "frontend/src/app/(dashboard)/patients/page.tsx").read_text(encoding="utf-8")
    form = (root / "frontend/src/app/(dashboard)/patients/patient-registration-form.tsx").read_text(
        encoding="utf-8"
    )
    actions = (root / "frontend/src/app/(dashboard)/patients/actions.ts").read_text(encoding="utf-8")
    route = (root / "backend/app/api/routes/crm.py").read_text(encoding="utf-8")

    assert "PatientRegistrationForm" in page
    assert 'pattern="01[0125][0-9]{8}"' in form
    assert "initial_package_offer_id" in form
    assert "initial_package_sessions_remaining" in form
    assert "initial_pulse_device_key" in form
    assert "initial_pulses_remaining" in form
    assert '"/crm/patients/register"' in actions
    assert '"/patients/register"' in route
    assert "create_patient_package_opening_balance" in route
    assert "create_patient_pulse_opening_balance" in route
    assert "db.commit()" in route


def test_registration_rolls_back_patient_and_package_if_pulse_balance_fails(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.api.routes.crm import register_patient
    from app.schemas.crm import PatientRegistrationCreate

    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)

    def reject_device(*args, **kwargs):
        raise pulse_billing_service.PulseBillingError("Device is not active.")

    monkeypatch.setattr(pulse_billing_service, "_active_clinic_laser_device", reject_device)
    engine = _engine()
    workspace_id, service_id = uuid4(), uuid4()

    with Session(engine) as db:
        service = Service(
            workspace_id=workspace_id,
            id=service_id,
            name="Laser",
            slug="laser",
            operational_category="dermatology",
            requires_laser_device=False,
            duration_minutes=30,
            price_minor=25_000,
        )
        offer = ServicePackageOffer(
            workspace_id=workspace_id,
            service_id=service_id,
            device_key=None,
            device_name=None,
            sessions_count=6,
            price_minor=120_000,
            currency="EGP",
            is_active=True,
        )
        db.add_all([service, offer])
        db.commit()

        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )
        payload = PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_package_offer_id=offer.id,
            initial_package_sessions_remaining=2,
            initial_pulse_device_key="missing",
            initial_pulses_remaining=500,
        )

        with pytest.raises(HTTPException) as error:
            register_patient(payload, access, db)

        assert error.value.status_code == 422
        assert db.scalar(select(func.count()).select_from(Patient)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPulsePack)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0
