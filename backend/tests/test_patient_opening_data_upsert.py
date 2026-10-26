from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    external_patient_fields,
    patient_field_authority_owner,
    require_tia_patient_fields_writable,
)
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.clinic_inventory import ClinicLaserDevice
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import AppointmentPulseSettlement, PatientPulsePack, PulseUsage
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.schemas.patient_opening_data import PatientOpeningDataUpdate
from app.services import package_offers as package_offer_service
from app.services import patient_opening_data as opening_service
from app.services import patient_packages as patient_package_service
from app.services import pulse_billing as pulse_billing_service


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    models = (
        Patient,
        Service,
        ClinicLaserDevice,
        ServicePackageOffer,
        PaymentTransaction,
        PatientPackage,
        PatientPulsePack,
        Appointment,
        AppointmentAdditionalService,
        AppointmentPulseSettlement,
        PackageUsage,
        PulseUsage,
    )
    excluded = [
        constraint
        for constraint in Appointment.__table__.constraints
        if constraint.__class__.__name__ == "ExcludeConstraint"
    ]
    for constraint in excluded:
        Appointment.__table__.constraints.remove(constraint)
    try:
        for model in models:
            model.__table__.create(engine)
    finally:
        for constraint in excluded:
            Appointment.__table__.constraints.add(constraint)
    return engine


@pytest.fixture(autouse=True)
def _disable_unrelated_side_effects(monkeypatch):
    monkeypatch.setattr(opening_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(patient_package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse_billing_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(patient_package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(opening_service, "require_tia_patient_fields_writable", lambda *args, **kwargs: None)


def _patient(db: Session, workspace_id: UUID, *, customer_id: str | None = None) -> Patient:
    patient = Patient(
        workspace_id=workspace_id,
        first_name="Mona",
        last_name="Ali",
        customer_id=customer_id,
        phone="01012345678",
        phone_normalized="+201012345678",
    )
    db.add(patient)
    db.flush()
    return patient


def _offer(
    db: Session,
    workspace_id: UUID,
    *,
    name: str,
    sessions: int = 6,
    price_minor: int = 600_000,
) -> ServicePackageOffer:
    service = Service(
        workspace_id=workspace_id,
        name=name,
        slug=f"{name.lower()}-{uuid4().hex[:8]}",
        operational_category="dermatology",
        requires_laser_device=False,
        duration_minutes=30,
        price_minor=100_000,
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


def _device(db: Session, workspace_id: UUID, key: str, name: str | None = None) -> ClinicLaserDevice:
    device = ClinicLaserDevice(
        workspace_id=workspace_id,
        device_key=key,
        name=name or key.title(),
        is_active=True,
    )
    db.add(device)
    db.flush()
    return device


def _opening_package(
    db: Session,
    workspace_id: UUID,
    patient_id: UUID,
    offer: ServicePackageOffer,
    *,
    remaining: int,
    amount_paid_minor: int = 0,
) -> PatientPackage:
    return package_offer_service.create_patient_package_opening_balance(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        offer_id=offer.id,
        sessions_remaining=remaining,
        amount_paid_minor=amount_paid_minor,
        payment_method="cash",
        created_by_user_id=None,
    )


def _opening_pulse(
    db: Session,
    workspace_id: UUID,
    patient_id: UUID,
    device_key: str,
    *,
    pulses: int,
) -> PatientPulsePack:
    return pulse_billing_service.create_patient_pulse_opening_balance(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        device_key=device_key,
        pulses_remaining=pulses,
        created_by_user_id=None,
    )


def _update(
    db: Session,
    workspace_id: UUID,
    patient_id: UUID,
    payload: dict,
):
    return opening_service.update_patient_opening_data(
        db,
        workspace_id=workspace_id,
        patient_id=patient_id,
        payload=PatientOpeningDataUpdate.model_validate(payload),
        actor_user_id=None,
    )


def test_o4_customer_id_is_replaced() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id, customer_id="A-1045")
        result = _update(
            db,
            workspace_id,
            patient.id,
            {"customer_id": "B-2010", "packages": [], "pulse_balances": []},
        )
        assert result.customer_id == "B-2010"
        assert patient.customer_id == "B-2010"


def test_o5_customer_id_explicit_external_authority_blocks_all_mutations(monkeypatch) -> None:
    integration = SimpleNamespace(
        mode="external_api",
        authority_policy_json={"patients": {"owner": "external", "fields": {"customer_id": "external"}}},
    )
    assert patient_field_authority_owner(integration, "customer_id") == "external"
    assert "customer_id" not in external_patient_fields(integration)
    default_external = SimpleNamespace(mode="external_api", authority_policy_json={})
    assert patient_field_authority_owner(default_external, "customer_id") == "tia"

    fake_db = SimpleNamespace(
        scalar=lambda stmt: uuid4(),
        get=lambda model, key: integration,
    )
    with pytest.raises(ClinicIntegrationAuthorityError, match="customer_id"):
        require_tia_patient_fields_writable(
            fake_db,
            workspace_id=uuid4(),
            patient_id=uuid4(),
            fields={"customer_id"},
        )

    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id, customer_id="OLD")
        _device(db, workspace_id, "candela")
        pack = _opening_pulse(db, workspace_id, patient.id, "candela", pulses=500)
        patient_id, pack_id = patient.id, pack.id
        db.commit()
        monkeypatch.setattr(
            opening_service,
            "require_tia_patient_fields_writable",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ClinicIntegrationAuthorityError("customer_id is externally owned")
            ),
        )
        with pytest.raises(ClinicIntegrationAuthorityError):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": "NEW",
                    "packages": [],
                    "pulse_balances": [{"device_key": "candela", "pulses_remaining": 350}],
                },
            )
        db.rollback()
        assert db.get(Patient, patient_id).customer_id == "OLD"
        assert db.get(PatientPulsePack, pack_id).pulses_purchased == 500


def test_o6_add_first_opening_package_creates_no_payment() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer = _offer(db, workspace_id, name="Full Body", sessions=6)
        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [{"offer_id": str(offer.id), "sessions_remaining": 4}],
                "pulse_balances": [],
            },
        )
        packages = list(db.scalars(select(PatientPackage).where(PatientPackage.patient_id == patient.id)))
        assert len(packages) == 1
        assert packages[0].opening_sessions_remaining == 4
        assert packages[0].sale_price_minor == offer.price_minor
        assert result.packages and result.packages[0].opening_sessions_remaining == 4
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0


def test_o7_o8_correct_same_package_preserves_usage_and_effective_remaining() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer = _offer(db, workspace_id, name="Bikini", sessions=6)
        package = _opening_package(db, workspace_id, patient.id, offer, remaining=4)
        usage = PackageUsage(
            workspace_id=workspace_id,
            patient_package_id=package.id,
            appointment_id=uuid4(),
            sessions_used=1,
            status="consumed",
            used_at=datetime.now(UTC),
        )
        db.add(usage)
        db.flush()

        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [{"package_id": str(package.id), "offer_id": str(offer.id), "sessions_remaining": 3}],
                "pulse_balances": [],
            },
        )
        assert db.scalar(select(func.count()).select_from(PatientPackage)) == 1
        assert db.get(PackageUsage, usage.id) is not None
        assert db.get(PatientPackage, package.id).opening_sessions_remaining == 3
        assert result.packages and result.packages[0].sessions_consumed == 1
        assert result.packages[0].current_remaining == 2


def test_o9_package_correction_cannot_drop_below_reserved_and_consumed() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer = _offer(db, workspace_id, name="Underarm", sessions=6)
        package = _opening_package(db, workspace_id, patient.id, offer, remaining=4)
        db.add(
            PackageUsage(
                workspace_id=workspace_id,
                patient_package_id=package.id,
                appointment_id=uuid4(),
                sessions_used=3,
                status="consumed",
                used_at=datetime.now(UTC),
            )
        )
        db.flush()
        with pytest.raises(opening_service.PatientOpeningDataConflict):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": None,
                    "packages": [{"package_id": str(package.id), "offer_id": str(offer.id), "sessions_remaining": 2}],
                    "pulse_balances": [],
                },
            )
        assert db.get(PatientPackage, package.id).opening_sessions_remaining == 4


def test_o10_pristine_package_identity_can_be_replaced_without_duplicate() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        old_offer = _offer(db, workspace_id, name="Bikini", sessions=6)
        new_offer = _offer(db, workspace_id, name="Underarm", sessions=6)
        old = _opening_package(db, workspace_id, patient.id, old_offer, remaining=4)
        old_id = old.id
        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [{"package_id": str(old.id), "offer_id": str(new_offer.id), "sessions_remaining": 5}],
                "pulse_balances": [],
            },
        )
        packages = list(db.scalars(select(PatientPackage).where(PatientPackage.patient_id == patient.id)))
        assert len(packages) == 1
        assert packages[0].id != old_id
        assert packages[0].package_offer_id == new_offer.id
        assert result.packages and result.packages[0].offer_id == new_offer.id


def test_o11_used_package_identity_replacement_is_blocked_and_history_survives() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        old_offer = _offer(db, workspace_id, name="Bikini", sessions=6)
        new_offer = _offer(db, workspace_id, name="Underarm", sessions=6)
        package = _opening_package(db, workspace_id, patient.id, old_offer, remaining=4)
        usage = PackageUsage(
            workspace_id=workspace_id,
            patient_package_id=package.id,
            appointment_id=uuid4(),
            sessions_used=1,
            status="released",
        )
        db.add(usage)
        db.flush()
        with pytest.raises(opening_service.PatientOpeningDataConflict, match="لا يمكن استبدال"):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": None,
                    "packages": [{"package_id": str(package.id), "offer_id": str(new_offer.id), "sessions_remaining": 4}],
                    "pulse_balances": [],
                },
            )
        assert db.get(PatientPackage, package.id) is not None
        assert db.get(PackageUsage, usage.id) is not None


def test_o12_financial_history_blocks_package_identity_replacement_but_same_balance_edit_preserves_it() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        old_offer = _offer(db, workspace_id, name="Old Package", sessions=6)
        new_offer = _offer(db, workspace_id, name="New Package", sessions=6)
        package = _opening_package(
            db, workspace_id, patient.id, old_offer, remaining=4, amount_paid_minor=100_000
        )
        db.flush()
        transactions_before = list(db.scalars(select(PaymentTransaction)))
        assert len(transactions_before) == 1

        same = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [{"package_id": str(package.id), "offer_id": str(old_offer.id), "sessions_remaining": 3}],
                "pulse_balances": [],
            },
        )
        assert same.packages and same.packages[0].opening_sessions_remaining == 3
        assert list(db.scalars(select(PaymentTransaction)))[0].id == transactions_before[0].id

        with pytest.raises(opening_service.PatientOpeningDataConflict):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": None,
                    "packages": [{"package_id": str(package.id), "offer_id": str(new_offer.id), "sessions_remaining": 3}],
                    "pulse_balances": [],
                },
            )
        assert db.get(PatientPackage, package.id) is not None
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 1


def test_o13_o14_add_and_correct_same_pulse_device_without_double_counting() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        _device(db, workspace_id, "candela", "Candela")
        added = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [],
                "pulse_balances": [{"device_key": "candela", "pulses_remaining": 5000}],
            },
        )
        assert len(added.pulse_balances) == 1
        assert db.scalar(select(func.count()).select_from(PaymentTransaction)) == 0
        pack = db.scalar(select(PatientPulsePack).where(PatientPulsePack.patient_id == patient.id))
        pack_id = pack.id

        corrected = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [],
                "pulse_balances": [{"device_key": "candela", "pulses_remaining": 3500}],
            },
        )
        assert db.scalar(select(func.count()).select_from(PatientPulsePack)) == 1
        assert db.get(PatientPulsePack, pack_id).pulses_purchased == 3500
        assert corrected.pulse_balances[0].opening_pulses == 3500


def test_o15_o16_pulse_usage_is_preserved_and_baseline_cannot_drop_below_consumption() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        _device(db, workspace_id, "candela")
        pack = _opening_pulse(db, workspace_id, patient.id, "candela", pulses=5000)
        usage = PulseUsage(
            workspace_id=workspace_id,
            patient_pulse_pack_id=pack.id,
            appointment_id=uuid4(),
            pulses_used=100,
            status="consumed",
            used_at=datetime.now(UTC),
        )
        db.add(usage)
        db.flush()
        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [],
                "pulse_balances": [{"device_key": "candela", "pulses_remaining": 3500}],
            },
        )
        assert db.get(PulseUsage, usage.id) is not None
        assert result.pulse_balances[0].current_remaining == 3400

        with pytest.raises(opening_service.PatientOpeningDataConflict):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": None,
                    "packages": [],
                    "pulse_balances": [{"device_key": "candela", "pulses_remaining": 50}],
                },
            )
        assert db.get(PatientPulsePack, pack.id).pulses_purchased == 3500


def test_o17_pristine_pulse_device_can_be_replaced() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        _device(db, workspace_id, "candela")
        _device(db, workspace_id, "deka")
        old = _opening_pulse(db, workspace_id, patient.id, "candela", pulses=3500)
        old_id = old.id
        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [],
                "pulse_balances": [{"device_key": "deka", "pulses_remaining": 3500}],
            },
        )
        packs = list(db.scalars(select(PatientPulsePack).where(PatientPulsePack.patient_id == patient.id)))
        assert len(packs) == 1
        assert packs[0].id != old_id
        assert packs[0].device_key == "deka"
        assert result.pulse_balances[0].device_key == "deka"


def test_o18_used_pulse_device_replacement_is_blocked() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        _device(db, workspace_id, "candela")
        _device(db, workspace_id, "deka")
        pack = _opening_pulse(db, workspace_id, patient.id, "candela", pulses=3500)
        usage = PulseUsage(
            workspace_id=workspace_id,
            patient_pulse_pack_id=pack.id,
            appointment_id=uuid4(),
            pulses_used=100,
            status="consumed",
            used_at=datetime.now(UTC),
        )
        db.add(usage)
        db.flush()
        with pytest.raises(opening_service.PatientOpeningDataConflict, match="لا يمكن تغيير جهاز"):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": None,
                    "packages": [],
                    "pulse_balances": [{"device_key": "deka", "pulses_remaining": 3500}],
                },
            )
        assert db.get(PatientPulsePack, pack.id) is not None
        assert db.get(PulseUsage, usage.id) is not None


def test_o19_purchased_pulse_pack_is_invisible_and_untouched() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        _device(db, workspace_id, "candela")
        opening = _opening_pulse(db, workspace_id, patient.id, "candela", pulses=1000)
        purchased = PatientPulsePack(
            workspace_id=workspace_id,
            patient_id=patient.id,
            pulse_pack_offer_id=uuid4(),
            origin_appointment_id=None,
            purchase_transaction_id=None,
            created_by_user_id=None,
            device_key="candela",
            device_name="Candela",
            pulses_purchased=2000,
            sale_price_minor=300_000,
            standalone_pulse_price_minor_at_purchase=200,
            currency="EGP",
            purchased_at=datetime.now(UTC),
            status="active",
        )
        db.add(purchased)
        db.flush()
        _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [],
                "pulse_balances": [{"device_key": "candela", "pulses_remaining": 900}],
            },
        )
        assert db.get(PatientPulsePack, opening.id).pulses_purchased == 900
        assert db.get(PatientPulsePack, purchased.id).pulses_purchased == 2000
        assert db.get(PatientPulsePack, purchased.id).sale_price_minor == 300_000


def test_o20_invalid_package_rejects_customer_id_and_pulse_changes_atomically() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id, customer_id="OLD")
        offer = _offer(db, workspace_id, name="Package", sessions=6)
        package = _opening_package(db, workspace_id, patient.id, offer, remaining=4)
        _device(db, workspace_id, "candela")
        pulse = _opening_pulse(db, workspace_id, patient.id, "candela", pulses=500)
        patient_id, package_id, pulse_id = patient.id, package.id, pulse.id
        db.commit()
        with pytest.raises(opening_service.PatientOpeningDataConflict):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": "NEW",
                    "packages": [{"package_id": str(package.id), "offer_id": str(offer.id), "sessions_remaining": 7}],
                    "pulse_balances": [{"device_key": "candela", "pulses_remaining": 350}],
                },
            )
        db.rollback()
        assert db.get(Patient, patient_id).customer_id == "OLD"
        assert db.get(PatientPackage, package_id).opening_sessions_remaining == 4
        assert db.get(PatientPulsePack, pulse_id).pulses_purchased == 500


def test_o21_cross_workspace_read_and_update_are_not_found() -> None:
    engine = _engine()
    workspace_a = uuid4()
    workspace_b = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_a, customer_id="PRIVATE")
        with pytest.raises(opening_service.PatientOpeningDataNotFound):
            opening_service.get_patient_opening_data(
                db, workspace_id=workspace_b, patient_id=patient.id
            )
        with pytest.raises(opening_service.PatientOpeningDataNotFound):
            _update(
                db,
                workspace_b,
                patient.id,
                {"customer_id": "LEAK", "packages": [], "pulse_balances": []},
            )
        assert db.get(Patient, patient.id).customer_id == "PRIVATE"


def test_opening_data_corrections_emit_explicit_staff_audit_events(monkeypatch) -> None:
    engine = _engine()
    workspace_id = uuid4()
    events: list[dict] = []
    monkeypatch.setattr(
        opening_service,
        "record_activity_event",
        lambda *args, **kwargs: events.append(kwargs),
    )
    with Session(engine) as db:
        patient = _patient(db, workspace_id, customer_id="OLD")
        offer = _offer(db, workspace_id, name="Audit Package", sessions=6)
        package = _opening_package(db, workspace_id, patient.id, offer, remaining=4)
        _device(db, workspace_id, "candela")
        _opening_pulse(db, workspace_id, patient.id, "candela", pulses=500)
        _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": "NEW",
                "packages": [{"package_id": str(package.id), "offer_id": str(offer.id), "sessions_remaining": 3}],
                "pulse_balances": [{"device_key": "candela", "pulses_remaining": 350}],
            },
        )

    actions = [event["action"] for event in events]
    assert "patient.opening_data_updated" in actions
    assert "package.opening_balance_corrected" in actions
    assert "pulse_pack.opening_balance_corrected" in actions
    patient_event = next(event for event in events if event["action"] == "patient.opening_data_updated")
    assert patient_event["metadata"] == {"customer_id_changed": True}
    assert "OLD" not in str(patient_event["metadata"])
    assert "NEW" not in str(patient_event["metadata"])
    assert package.id is not None

def test_multi_o1_get_returns_two_opening_packages() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="Underarm")
        offer_b = _offer(db, workspace_id, name="Bikini")
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=5)
        result = opening_service.get_patient_opening_data(db, workspace_id=workspace_id, patient_id=patient.id)
        assert {item.package_id for item in result.packages} == {package_a.id, package_b.id}
        assert {item.offer_id for item in result.packages} == {offer_a.id, offer_b.id}


def test_multi_o2_correct_package_a_preserves_package_b_and_its_payment_history() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="Underarm", price_minor=300_000)
        offer_b = _offer(db, workspace_id, name="Bikini", price_minor=400_000)
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=5, amount_paid_minor=100_000)
        payment = db.scalar(select(PaymentTransaction).where(PaymentTransaction.patient_package_id == package_b.id))
        assert payment is not None
        payment_id = payment.id
        refund = PaymentTransaction(
            workspace_id=workspace_id,
            appointment_id=None,
            origin_appointment_id=None,
            patient_id=patient.id,
            created_by_user_id=None,
            reference_transaction_id=payment.id,
            patient_package_id=package_b.id,
            transaction_type="refund",
            amount_minor=25_000,
            currency="EGP",
            payment_method=payment.payment_method,
            source="staff",
            reason="Historical correction",
            created_at=datetime.now(UTC),
        )
        db.add(refund)
        db.flush()
        refund_id = refund.id
        _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [
                    {"package_id": str(package_a.id), "offer_id": str(offer_a.id), "sessions_remaining": 3},
                    {"package_id": str(package_b.id), "offer_id": str(offer_b.id), "sessions_remaining": 5},
                ],
                "pulse_balances": [],
            },
        )
        assert db.get(PatientPackage, package_a.id).opening_sessions_remaining == 3
        assert db.get(PatientPackage, package_b.id).opening_sessions_remaining == 5
        assert db.get(PaymentTransaction, payment_id).patient_package_id == package_b.id
        assert db.get(PaymentTransaction, payment_id).amount_minor == 100_000
        assert db.get(PaymentTransaction, refund_id).patient_package_id == package_b.id
        assert db.get(PaymentTransaction, refund_id).reference_transaction_id == payment_id
        assert db.get(PaymentTransaction, refund_id).amount_minor == 25_000


def test_multi_o3_add_package_c_preserves_a_and_b() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="A")
        offer_b = _offer(db, workspace_id, name="B")
        offer_c = _offer(db, workspace_id, name="C")
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=5)
        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [
                    {"package_id": str(package_a.id), "offer_id": str(offer_a.id), "sessions_remaining": 4},
                    {"package_id": str(package_b.id), "offer_id": str(offer_b.id), "sessions_remaining": 5},
                    {"offer_id": str(offer_c.id), "sessions_remaining": 2},
                ],
                "pulse_balances": [],
            },
        )
        assert {item.package_id for item in result.packages}.issuperset({package_a.id, package_b.id})
        assert {item.offer_id for item in result.packages} == {offer_a.id, offer_b.id, offer_c.id}


def test_multi_o4_replace_pristine_package_a_preserves_b() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="A")
        offer_b = _offer(db, workspace_id, name="B")
        offer_c = _offer(db, workspace_id, name="C")
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=5)
        old_a_id = package_a.id
        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [
                    {"package_id": str(package_a.id), "offer_id": str(offer_c.id), "sessions_remaining": 3},
                    {"package_id": str(package_b.id), "offer_id": str(offer_b.id), "sessions_remaining": 5},
                ],
                "pulse_balances": [],
            },
        )
        assert db.get(PatientPackage, old_a_id) is None
        assert db.get(PatientPackage, package_b.id) is not None
        assert {item.offer_id for item in result.packages} == {offer_b.id, offer_c.id}


def test_multi_o5_replace_used_package_a_is_blocked_without_touching_b() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="A")
        offer_b = _offer(db, workspace_id, name="B")
        offer_c = _offer(db, workspace_id, name="C")
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=5)
        usage = PackageUsage(
            workspace_id=workspace_id,
            patient_package_id=package_a.id,
            appointment_id=uuid4(),
            sessions_used=1,
            status="consumed",
            used_at=datetime.now(UTC),
        )
        db.add(usage)
        db.flush()
        with pytest.raises(opening_service.PatientOpeningDataConflict):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": None,
                    "packages": [
                        {"package_id": str(package_a.id), "offer_id": str(offer_c.id), "sessions_remaining": 4},
                        {"package_id": str(package_b.id), "offer_id": str(offer_b.id), "sessions_remaining": 5},
                    ],
                    "pulse_balances": [],
                },
            )
        assert db.get(PatientPackage, package_a.id) is not None
        assert db.get(PatientPackage, package_b.id) is not None
        assert db.get(PackageUsage, usage.id) is not None


def test_multi_o6_remove_pristine_package_a_is_allowed() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="A")
        offer_b = _offer(db, workspace_id, name="B")
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=5)
        result = _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [
                    {"package_id": str(package_b.id), "offer_id": str(offer_b.id), "sessions_remaining": 5}
                ],
                "pulse_balances": [],
            },
        )
        assert db.get(PatientPackage, package_a.id) is None
        assert db.get(PatientPackage, package_b.id) is not None
        assert [item.package_id for item in result.packages] == [package_b.id]


def test_multi_o7_remove_used_package_a_is_blocked() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="A")
        offer_b = _offer(db, workspace_id, name="B")
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=5)
        usage = PackageUsage(
            workspace_id=workspace_id,
            patient_package_id=package_a.id,
            appointment_id=uuid4(),
            sessions_used=1,
            status="reserved",
        )
        db.add(usage)
        db.flush()
        with pytest.raises(opening_service.PatientOpeningDataConflict):
            _update(
                db,
                workspace_id,
                patient.id,
                {
                    "customer_id": None,
                    "packages": [
                        {"package_id": str(package_b.id), "offer_id": str(offer_b.id), "sessions_remaining": 5}
                    ],
                    "pulse_balances": [],
                },
            )
        assert db.get(PatientPackage, package_a.id) is not None
        assert db.get(PatientPackage, package_b.id) is not None


def test_multi_o8_purchased_package_beside_opening_packages_is_untouched() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        offer_a = _offer(db, workspace_id, name="Opening")
        opening = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        purchased_service = Service(
            workspace_id=workspace_id,
            name="Purchased",
            slug=f"purchased-{uuid4().hex[:8]}",
            operational_category="dermatology",
            requires_laser_device=False,
            duration_minutes=30,
            price_minor=50_000,
        )
        db.add(purchased_service)
        db.flush()
        purchased = patient_package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient.id,
            service_id=purchased_service.id,
            name="Purchased 6",
            sessions_purchased=6,
            sale_price_minor=300_000,
            amount_paid_minor=0,
            payment_method="unknown",
            created_by_user_id=None,
            opening_sessions_remaining=None,
        )
        purchased_id = purchased.id
        _update(
            db,
            workspace_id,
            patient.id,
            {
                "customer_id": None,
                "packages": [
                    {"package_id": str(opening.id), "offer_id": str(offer_a.id), "sessions_remaining": 3}
                ],
                "pulse_balances": [],
            },
        )
        assert db.get(PatientPackage, purchased_id) is not None
        assert db.get(PatientPackage, purchased_id).opening_sessions_remaining is None
        assert db.get(PatientPackage, purchased_id).sessions_purchased == 6


def test_multi_o9_invalid_one_of_customer_packages_and_pulse_rolls_back_all() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id, customer_id="OLD")
        offer_a = _offer(db, workspace_id, name="A", sessions=6)
        offer_b = _offer(db, workspace_id, name="B", sessions=4)
        package_a = _opening_package(db, workspace_id, patient.id, offer_a, remaining=4)
        package_b = _opening_package(db, workspace_id, patient.id, offer_b, remaining=3)
        _device(db, workspace_id, "candela")
        pulse = _opening_pulse(db, workspace_id, patient.id, "candela", pulses=500)
        patient_id, a_id, b_id, pulse_id = patient.id, package_a.id, package_b.id, pulse.id
        db.commit()
        with pytest.raises(opening_service.PatientOpeningDataConflict):
            _update(
                db,
                workspace_id,
                patient_id,
                {
                    "customer_id": "NEW",
                    "packages": [
                        {"package_id": str(a_id), "offer_id": str(offer_a.id), "sessions_remaining": 3},
                        {"package_id": str(b_id), "offer_id": str(offer_b.id), "sessions_remaining": 5},
                    ],
                    "pulse_balances": [{"device_key": "candela", "pulses_remaining": 350}],
                },
            )
        db.rollback()
        assert db.get(Patient, patient_id).customer_id == "OLD"
        assert db.get(PatientPackage, a_id).opening_sessions_remaining == 4
        assert db.get(PatientPackage, b_id).opening_sessions_remaining == 3
        assert db.get(PatientPulsePack, pulse_id).pulses_purchased == 500


def test_opening_data_rejects_removed_singular_package_contract() -> None:
    with pytest.raises(ValidationError):
        PatientOpeningDataUpdate.model_validate(
            {
                "customer_id": None,
                "package": {"offer_id": str(uuid4()), "sessions_remaining": 2},
                "pulse_balances": [],
            }
        )
