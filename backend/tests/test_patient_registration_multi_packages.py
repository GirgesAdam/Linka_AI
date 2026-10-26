from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.routes.crm import register_patient
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import PatientPulsePack
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.schemas.crm import PatientRegistrationCreate
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


@pytest.fixture(autouse=True)
def _disable_external_side_effects(monkeypatch):
    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(patient_package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pulse_billing_service,
        "_active_clinic_laser_device",
        lambda db, *, workspace_id, device_key: SimpleNamespace(device_key=device_key, name=device_key.title()),
    )


def _access(workspace_id: UUID):
    return SimpleNamespace(workspace=SimpleNamespace(id=workspace_id), user=SimpleNamespace(id=uuid4()))


def _offer(
    db: Session,
    workspace_id: UUID,
    *,
    name: str,
    sessions: int = 6,
    price_minor: int = 300_000,
) -> ServicePackageOffer:
    service = Service(
        workspace_id=workspace_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid4().hex[:8]}",
        operational_category="dermatology",
        requires_laser_device=False,
        duration_minutes=30,
        price_minor=50_000,
    )
    db.add(service)
    db.flush()
    offer = ServicePackageOffer(
        workspace_id=workspace_id,
        service_id=service.id,
        sessions_count=sessions,
        price_minor=price_minor,
        currency="EGP",
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _package_payload(offer: ServicePackageOffer, *, remaining: int, paid: int, method: str = "cash"):
    return {
        "offer_id": offer.id,
        "sessions_remaining": remaining,
        "amount_paid_minor": paid,
        "payment_method": method,
    }


def test_r1_n1_registration_with_zero_packages_stores_single_full_name() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = register_patient(
            PatientRegistrationCreate(
                first_name="منى علي حسن",
                last_name=None,
                phone="01012345678",
            ),
            _access(workspace_id),
            db,
        )
        assert patient.first_name == "منى علي حسن"
        assert patient.last_name is None
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0


def test_r2_registration_with_one_package_creates_one_opening_package() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        offer = _offer(db, workspace_id, name="Underarm")
        patient = register_patient(
            PatientRegistrationCreate(
                first_name="Mona",
                phone="01012345678",
                initial_packages=[_package_payload(offer, remaining=4, paid=0)],
            ),
            _access(workspace_id),
            db,
        )
        packages = list(db.scalars(select(PatientPackage).where(PatientPackage.patient_id == patient.id)))
        assert len(packages) == 1
        assert packages[0].package_offer_id == offer.id
        assert packages[0].opening_sessions_remaining == 4


def test_r3_r4_two_packages_keep_entitlements_and_payments_independent() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        offer_a = _offer(db, workspace_id, name="Underarm", sessions=6, price_minor=200_000)
        offer_b = _offer(db, workspace_id, name="Bikini", sessions=8, price_minor=300_000)
        patient = register_patient(
            PatientRegistrationCreate(
                first_name="Mona",
                phone="01012345678",
                initial_packages=[
                    _package_payload(offer_a, remaining=4, paid=100_000, method="cash"),
                    _package_payload(offer_b, remaining=6, paid=250_000, method="visa"),
                ],
            ),
            _access(workspace_id),
            db,
        )
        packages = list(db.scalars(select(PatientPackage).where(PatientPackage.patient_id == patient.id)))
        assert len(packages) == 2
        by_offer = {row.package_offer_id: row for row in packages}
        assert by_offer[offer_a.id].opening_sessions_remaining == 4
        assert by_offer[offer_b.id].opening_sessions_remaining == 6

        payments = list(db.scalars(select(PaymentTransaction).where(PaymentTransaction.patient_id == patient.id)))
        assert len(payments) == 2
        by_package = {row.patient_package_id: row for row in payments}
        assert (by_package[by_offer[offer_a.id].id].amount_minor, by_package[by_offer[offer_a.id].id].payment_method) == (100_000, "cash")
        assert (by_package[by_offer[offer_b.id].id].amount_minor, by_package[by_offer[offer_b.id].id].payment_method) == (250_000, "visa")
        assert by_package[by_offer[offer_a.id].id].patient_package_id != by_offer[offer_b.id].id
        assert by_package[by_offer[offer_b.id].id].patient_package_id != by_offer[offer_a.id].id


def test_r5_zero_package_payment_creates_no_fake_positive_transaction() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        offer = _offer(db, workspace_id, name="Zero Paid", price_minor=300_000)
        patient = register_patient(
            PatientRegistrationCreate(
                first_name="Mona",
                phone="01012345678",
                initial_packages=[_package_payload(offer, remaining=3, paid=0)],
            ),
            _access(workspace_id),
            db,
        )
        assert db.scalar(select(func.count()).select_from(PatientPackage).where(PatientPackage.patient_id == patient.id)) == 1
        assert db.scalar(select(func.count()).select_from(PaymentTransaction).where(PaymentTransaction.patient_id == patient.id)) == 0


def test_r6_invalid_second_package_rolls_back_patient_first_package_and_payment() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        offer_a = _offer(db, workspace_id, name="Valid", sessions=6, price_minor=200_000)
        offer_b = _offer(db, workspace_id, name="Invalid", sessions=4, price_minor=300_000)
        db.commit()
        with pytest.raises(HTTPException) as error:
            register_patient(
                PatientRegistrationCreate(
                    first_name="Mona",
                    phone="01012345678",
                    initial_packages=[
                        _package_payload(offer_a, remaining=4, paid=100_000),
                        _package_payload(offer_b, remaining=5, paid=100_000),
                    ],
                ),
                _access(workspace_id),
                db,
            )
        assert error.value.status_code == 422
        assert db.scalar(select(func.count()).select_from(Patient)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_r7_same_offer_submitted_twice_is_rejected_by_contract() -> None:
    offer_id = uuid4()
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Mona",
            phone="01012345678",
            initial_packages=[
                {"offer_id": offer_id, "sessions_remaining": 3, "amount_paid_minor": 0},
                {"offer_id": offer_id, "sessions_remaining": 2, "amount_paid_minor": 0},
            ],
        )


def test_r8_packages_and_pulse_balances_commit_atomically() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        offer_a = _offer(db, workspace_id, name="A")
        offer_b = _offer(db, workspace_id, name="B")
        patient = register_patient(
            PatientRegistrationCreate(
                first_name="Mona",
                phone="01012345678",
                initial_packages=[
                    _package_payload(offer_a, remaining=4, paid=0),
                    _package_payload(offer_b, remaining=3, paid=0),
                ],
                initial_pulse_balances=[
                    {"device_key": "candela", "pulses_remaining": 500},
                    {"device_key": "alma", "pulses_remaining": 750},
                ],
            ),
            _access(workspace_id),
            db,
        )
        assert db.scalar(select(func.count()).select_from(PatientPackage).where(PatientPackage.patient_id == patient.id)) == 2
        assert db.scalar(select(func.count()).select_from(PatientPulsePack).where(PatientPulsePack.patient_id == patient.id)) == 2


def test_r9_duplicate_phone_with_full_entitlements_causes_zero_mutation() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        offer_a = _offer(db, workspace_id, name="A")
        offer_b = _offer(db, workspace_id, name="B")
        existing = Patient(
            workspace_id=workspace_id,
            first_name="Existing",
            phone="01012345678",
            phone_normalized="01012345678",
        )
        db.add(existing)
        db.commit()
        with pytest.raises(HTTPException) as error:
            register_patient(
                PatientRegistrationCreate(
                    first_name="Duplicate",
                    phone="01012345678",
                    initial_packages=[
                        _package_payload(offer_a, remaining=4, paid=100_000),
                        _package_payload(offer_b, remaining=3, paid=100_000),
                    ],
                    initial_pulse_balances=[{"device_key": "candela", "pulses_remaining": 500}],
                ),
                _access(workspace_id),
                db,
            )
        assert error.value.status_code == 409
        assert db.scalar(select(func.count()).select_from(Patient)) == 1
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPulsePack)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_r10_cross_workspace_offer_rejected_with_zero_partial_registration() -> None:
    engine = _engine()
    workspace_a, workspace_b = uuid4(), uuid4()
    with Session(engine) as db:
        foreign_offer = _offer(db, workspace_b, name="Foreign")
        db.commit()
        with pytest.raises(HTTPException) as error:
            register_patient(
                PatientRegistrationCreate(
                    first_name="Mona",
                    phone="01012345678",
                    initial_packages=[_package_payload(foreign_offer, remaining=2, paid=0)],
                ),
                _access(workspace_a),
                db,
            )
        assert error.value.status_code == 404
        assert db.scalar(select(func.count()).select_from(Patient)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0
