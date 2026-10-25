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
            amount_paid_minor=0,
            payment_method="cash",
            created_by_user_id=None,
        )

        assert package.sessions_purchased == 6
        assert package.opening_sessions_remaining == 2
        assert package.sessions_total_known is True
        assert package.sale_price_minor == offer.price_minor
        assert package.purchase_transaction_id is None
        financial_read = patient_package_service.package_read(db, package, include_financials=True)
        assert financial_read.amount_paid_minor == 0
        assert financial_read.balance_due_minor == offer.price_minor
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
    monkeypatch.setattr(patient_package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pulse_billing_service,
        "_active_clinic_laser_device",
        lambda db, *, workspace_id, device_key: SimpleNamespace(device_key=device_key, name=device_key.title()),
    )
    original_resolve_device = pulse_billing_service._active_clinic_laser_device

    def reject_second_device(db, *, workspace_id, device_key):
        if device_key == "invalid":
            raise pulse_billing_service.PulseBillingError("Device is not active.")
        return original_resolve_device(db, workspace_id=workspace_id, device_key=device_key)

    monkeypatch.setattr(pulse_billing_service, "_active_clinic_laser_device", reject_second_device)
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
            customer_id="000012",
            initial_package_offer_id=offer.id,
            initial_package_sessions_remaining=2,
            initial_package_amount_paid_minor=120_000,
            initial_package_payment_method="cash",
            initial_pulse_balances=[
                {"device_key": "candela", "pulses_remaining": 500},
                {"device_key": "invalid", "pulses_remaining": 250},
            ],
        )

        with pytest.raises(HTTPException) as error:
            register_patient(payload, access, db)

        assert error.value.status_code == 422
        assert db.scalar(select(func.count()).select_from(Patient)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPulsePack)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_opening_package_balance_defaults_to_full_offer_price_and_records_one_payment(monkeypatch) -> None:
    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(patient_package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
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
            sessions_count=8,
            price_minor=600_000,
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
            sessions_remaining=5,
            created_by_user_id=None,
        )
        db.flush()

        transactions = list(db.scalars(select(PaymentTransaction)))
        financial_read = patient_package_service.package_read(db, package, include_financials=True)
        assert package.sale_price_minor == 600_000
        assert package.opening_sessions_remaining == 5
        assert package.sessions_purchased == 8
        assert len(transactions) == 1
        assert transactions[0].amount_minor == 600_000
        assert transactions[0].payment_method == "cash"
        assert transactions[0].patient_package_id == package.id
        assert financial_read.amount_paid_minor == 600_000
        assert financial_read.balance_due_minor == 0


def test_registration_records_partial_package_payment_and_multiple_independent_pulse_balances(monkeypatch) -> None:
    from app.api.routes.crm import register_patient
    from app.schemas.crm import PatientRegistrationCreate

    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(patient_package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pulse_billing_service,
        "_active_clinic_laser_device",
        lambda db, *, workspace_id, device_key: SimpleNamespace(device_key=device_key, name=device_key.title()),
    )
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
            sessions_count=8,
            price_minor=600_000,
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
            customer_id="000012",
            initial_package_offer_id=offer.id,
            initial_package_sessions_remaining=4,
            initial_package_amount_paid_minor=400_000,
            initial_package_payment_method="visa",
            initial_pulse_balances=[
                {"device_key": "candela", "pulses_remaining": 500},
                {"device_key": "alma", "pulses_remaining": 750},
            ],
        )

        patient = register_patient(payload, access, db)
        package = db.scalar(select(PatientPackage).where(PatientPackage.patient_id == patient.id))
        assert package is not None
        financial_read = patient_package_service.package_read(db, package, include_financials=True)
        assert patient.customer_id == "000012"
        assert package.sale_price_minor == 600_000
        assert package.sessions_purchased == 8
        assert financial_read.sessions_remaining == 4
        assert financial_read.amount_paid_minor == 400_000
        assert financial_read.balance_due_minor == 200_000

        pulse_packs = list(db.scalars(select(PatientPulsePack).where(PatientPulsePack.patient_id == patient.id)))
        assert {(pack.device_key, pack.pulses_purchased) for pack in pulse_packs} == {
            ("candela", 500),
            ("alma", 750),
        }
        assert all(pack.purchase_transaction_id is None for pack in pulse_packs)
        assert all(pack.pulse_pack_offer_id is None for pack in pulse_packs)
        transactions = list(db.scalars(select(PaymentTransaction).where(PaymentTransaction.patient_id == patient.id)))
        assert len(transactions) == 1
        assert transactions[0].amount_minor == 400_000
        assert transactions[0].payment_method == "visa"
        assert transactions[0].patient_package_id == package.id


def test_zero_package_payment_keeps_offer_price_and_creates_no_payment(monkeypatch) -> None:
    from app.api.routes.crm import register_patient
    from app.schemas.crm import PatientRegistrationCreate

    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)
    engine = _engine()
    workspace_id, service_id = uuid4(), uuid4()

    with Session(engine) as db:
        service = Service(
            workspace_id=workspace_id,
            id=service_id,
            name="Dermatology",
            slug="dermatology",
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
            price_minor=600_000,
            currency="EGP",
            is_active=True,
        )
        db.add_all([service, offer])
        db.commit()
        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )
        patient = register_patient(
            PatientRegistrationCreate(
                first_name="Ada",
                phone="01112345678",
                initial_package_offer_id=offer.id,
                initial_package_sessions_remaining=3,
                initial_package_amount_paid_minor=0,
            ),
            access,
            db,
        )
        package = db.scalar(select(PatientPackage).where(PatientPackage.patient_id == patient.id))
        assert package is not None
        financial_read = patient_package_service.package_read(db, package, include_financials=True)
        assert package.sale_price_minor == 600_000
        assert financial_read.sessions_remaining == 3
        assert financial_read.amount_paid_minor == 0
        assert financial_read.balance_due_minor == 600_000
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_invalid_opening_package_balance_rolls_back_patient_creation(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.api.routes.crm import register_patient
    from app.schemas.crm import PatientRegistrationCreate

    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(patient_package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
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
        with pytest.raises(HTTPException) as error:
            register_patient(
                PatientRegistrationCreate(
                    first_name="Ada",
                    phone="01212345678",
                    initial_package_offer_id=offer.id,
                    initial_package_sessions_remaining=7,
                    initial_package_amount_paid_minor=120_000,
                ),
                access,
                db,
            )
        assert error.value.status_code == 422
        assert db.scalar(select(func.count()).select_from(Patient)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_registration_with_multiple_pulse_balances_creates_no_payment_transactions(monkeypatch) -> None:
    from app.api.routes.crm import register_patient
    from app.schemas.crm import PatientRegistrationCreate

    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pulse_billing_service,
        "_active_clinic_laser_device",
        lambda db, *, workspace_id, device_key: SimpleNamespace(device_key=device_key, name=device_key.title()),
    )
    engine = _engine()
    workspace_id = uuid4()

    with Session(engine) as db:
        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )
        patient = register_patient(
            PatientRegistrationCreate(
                first_name="Pulse",
                phone="01512345678",
                initial_pulse_balances=[
                    {"device_key": "candela", "pulses_remaining": 500},
                    {"device_key": "alma", "pulses_remaining": 750},
                ],
            ),
            access,
            db,
        )

        pulse_packs = list(
            db.scalars(
                select(PatientPulsePack).where(PatientPulsePack.patient_id == patient.id)
            )
        )
        assert {(pack.device_key, pack.pulses_purchased) for pack in pulse_packs} == {
            ("candela", 500),
            ("alma", 750),
        }
        assert all(pack.purchase_transaction_id is None for pack in pulse_packs)
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_duplicate_registration_returns_existing_patient_link_target_without_new_row() -> None:
    from fastapi import HTTPException

    from app.api.routes.crm import DUPLICATE_PATIENT_MESSAGE, register_patient
    from app.schemas.crm import PatientRegistrationCreate

    engine = _engine()
    workspace_id = uuid4()

    with Session(engine) as db:
        existing = Patient(
            workspace_id=workspace_id,
            first_name="Mona",
            last_name="Ali",
            phone="01012345678",
            phone_normalized="01012345678",
        )
        db.add(existing)
        db.commit()
        existing_id = existing.id
        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )

        with pytest.raises(HTTPException) as error:
            register_patient(
                PatientRegistrationCreate(first_name="Duplicate", phone="01012345678"),
                access,
                db,
            )

        assert error.value.status_code == 409
        assert DUPLICATE_PATIENT_MESSAGE == "\u0631\u0642\u0645 \u0627\u0644\u0645\u0648\u0628\u0627\u064a\u0644 \u062f\u0647 \u0645\u0633\u062c\u0644 \u0628\u0627\u0644\u0641\u0639\u0644 \u0644\u0639\u0645\u064a\u0644 \u0645\u0648\u062c\u0648\u062f."
        assert error.value.detail == {
            "code": "patient_phone_exists",
            "message": DUPLICATE_PATIENT_MESSAGE,
            "patient_id": str(existing_id),
            "patient_name": "Mona Ali",
        }
        assert db.scalar(select(func.count()).select_from(Patient)) == 1


def test_duplicate_phone_lookup_is_scoped_to_current_workspace() -> None:
    from app.api.routes.crm import register_patient
    from app.schemas.crm import PatientRegistrationCreate

    engine = _engine()
    workspace_id = uuid4()
    other_workspace_id = uuid4()

    with Session(engine) as db:
        db.add(
            Patient(
                workspace_id=other_workspace_id,
                first_name="Other",
                last_name="Workspace",
                phone="01112345678",
                phone_normalized="01112345678",
            )
        )
        db.commit()
        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )

        created = register_patient(
            PatientRegistrationCreate(first_name="New", phone="01112345678"),
            access,
            db,
        )

        assert created.workspace_id == workspace_id
        assert created.first_name == "New"
        assert db.scalar(select(func.count()).select_from(Patient)) == 2



def test_integrity_error_race_fallback_returns_current_workspace_patient(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.api.routes import crm as crm_routes
    from app.schemas.crm import PatientRegistrationCreate

    engine = _engine()
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    phone = "01077777777"

    with Session(engine) as db:
        canonical = Patient(
            workspace_id=workspace_id,
            first_name="Mona",
            last_name="Ali",
            phone=phone,
            phone_normalized=phone,
        )
        foreign = Patient(
            workspace_id=other_workspace_id,
            first_name="Foreign",
            last_name="Patient",
            phone=phone,
            phone_normalized=phone,
        )
        db.add_all([canonical, foreign])
        db.commit()
        canonical_id = canonical.id
        foreign_id = foreign.id

        original_lookup = crm_routes.existing_patient_by_phone
        lookup_calls = 0

        def race_lookup(db, *, workspace_id, phone):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return None
            return original_lookup(db, workspace_id=workspace_id, phone=phone)

        monkeypatch.setattr(crm_routes, "existing_patient_by_phone", race_lookup)
        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )

        with pytest.raises(HTTPException) as error:
            crm_routes.register_patient(
                PatientRegistrationCreate(first_name="Race", phone=phone),
                access,
                db,
            )

        assert lookup_calls == 2
        assert error.value.status_code == 409
        assert error.value.detail == {
            "code": "patient_phone_exists",
            "message": crm_routes.DUPLICATE_PATIENT_MESSAGE,
            "patient_id": str(canonical_id),
            "patient_name": "Mona Ali",
        }
        assert error.value.detail["patient_id"] != str(foreign_id)
        assert error.value.detail["patient_name"] != "Foreign Patient"
        assert db.scalar(select(func.count()).select_from(Patient)) == 2
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPulsePack)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_phone_conflict_fallback_does_not_expose_other_workspace_patient(monkeypatch) -> None:
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    from app.api.routes import crm as crm_routes
    from app.schemas.crm import PatientRegistrationCreate

    engine = _engine()
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    phone = "01177777777"

    with Session(engine) as db:
        foreign = Patient(
            workspace_id=other_workspace_id,
            first_name="Foreign",
            last_name="Patient",
            phone=phone,
            phone_normalized=phone,
        )
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id

        original_lookup = crm_routes.existing_patient_by_phone
        lookup_calls = 0

        def scoped_lookup(db, *, workspace_id, phone):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 1:
                return None
            return original_lookup(db, workspace_id=workspace_id, phone=phone)

        original_flush = db.flush
        flush_calls = 0

        def force_phone_conflict(*args, **kwargs):
            nonlocal flush_calls
            flush_calls += 1
            if flush_calls == 1:
                raise IntegrityError(
                    "INSERT INTO patients ...",
                    {},
                    Exception("UNIQUE constraint failed: patients.workspace_id, patients.phone_normalized"),
                )
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(crm_routes, "existing_patient_by_phone", scoped_lookup)
        monkeypatch.setattr(db, "flush", force_phone_conflict)
        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )

        with pytest.raises(HTTPException) as error:
            crm_routes.register_patient(
                PatientRegistrationCreate(first_name="New", phone=phone),
                access,
                db,
            )

        assert error.value.status_code == 409
        assert error.value.detail == "Registration conflicts with existing clinic data. Refresh and try again."
        assert str(foreign_id) not in str(error.value.detail)
        assert "Foreign Patient" not in str(error.value.detail)
        assert db.scalar(select(func.count()).select_from(Patient)) == 1
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPulsePack)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_unrelated_integrity_error_stays_generic_without_patient_metadata(monkeypatch) -> None:
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    from app.api.routes import crm as crm_routes
    from app.schemas.crm import PatientRegistrationCreate

    engine = _engine()
    workspace_id = uuid4()

    with Session(engine) as db:
        original_flush = db.flush
        flush_calls = 0

        def force_unrelated_conflict(*args, **kwargs):
            nonlocal flush_calls
            flush_calls += 1
            if flush_calls == 1:
                raise IntegrityError(
                    "INSERT INTO patients ...",
                    {},
                    Exception("some_unrelated_registration_constraint"),
                )
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(crm_routes, "existing_patient_by_phone", lambda *args, **kwargs: None)
        monkeypatch.setattr(db, "flush", force_unrelated_conflict)
        access = SimpleNamespace(
            workspace=SimpleNamespace(id=workspace_id),
            user=SimpleNamespace(id=uuid4()),
        )

        with pytest.raises(HTTPException) as error:
            crm_routes.register_patient(
                PatientRegistrationCreate(first_name="Conflict", phone="01277777777"),
                access,
                db,
            )

        assert error.value.status_code == 409
        assert error.value.detail == "Registration conflicts with existing clinic data. Refresh and try again."
        assert not isinstance(error.value.detail, dict)
        assert "patient_id" not in str(error.value.detail)
        assert "patient_name" not in str(error.value.detail)
        assert db.scalar(select(func.count()).select_from(Patient)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 0
        assert db.scalar(select(func.count()).select_from(PatientPulsePack)) == 0
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0
