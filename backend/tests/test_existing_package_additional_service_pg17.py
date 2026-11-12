"""Existing patient package -> additional appointment service on disposable PG17.

This suite NEVER connects to a configured application, Supabase, or shared DB.
Provide an isolated localhost-only EXISTING_PACKAGE_TEST_DATABASE_URL manually.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes import appointment_commerce as appointment_route
from app.api.routes import payments as payment_route
from app.database.base import Base
from app.database.session import get_db
from app.models import (
    Appointment,
    AppointmentAdditionalService,
    AppointmentProductLine,
    Branch,
    ClinicLaserDevice,
    ClinicProduct,
    Doctor,
    DoctorService,
    PackageUsage,
    Patient,
    PatientPackage,
    PaymentAllocation,
    PaymentTransaction,
    PulseBillingSettings,
    Service,
    ServiceDevicePrice,
    Staff,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services import appointment_commerce as commerce
from app.services.patient_packages import consume_visit_package_usages, package_read


@pytest.fixture(scope="module")
def engine():
    raw = os.environ.get("EXISTING_PACKAGE_TEST_DATABASE_URL")
    if not raw:
        if os.environ.get("CI"):
            pytest.fail("CI must provision a disposable PG17 package-service database; skipping is forbidden")
        pytest.skip("Requires explicitly configured disposable PG17 test DB")
    url = make_url(raw)
    assert url.host in {"127.0.0.1", "localhost"}
    assert url.port == 55432
    assert url.database == "package_service_ci"
    assert url.username == "package_ci_user"
    db_engine = create_engine(raw, pool_pre_ping=True)
    tables = {
        "workspaces", "users", "workspace_members", "staff", "doctors",
        "doctor_services", "branches", "patients", "services", "appointments",
        "patient_packages", "package_usages", "appointment_additional_services",
        "clinic_laser_devices", "service_device_prices", "activity_events",
        "payment_transactions", "payment_allocations",
        "appointment_product_lines", "patient_pulse_packs",
        "appointment_pulse_settlements", "pulse_billing_settings", "pulse_usages",
    }
    while True:
        before = len(tables)
        for name in tuple(tables):
            tables.update(fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys)
        if len(tables) == before:
            break
    Base.metadata.create_all(db_engine, tables=[Base.metadata.tables[name] for name in tables])
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture()
def ctx(engine, monkeypatch):
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    start = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    purchased = start - timedelta(days=10)
    with sessions() as db:
        suffix = uuid4().hex[:10]
        workspace = Workspace(name="Package Regression Workspace", slug="pkg-reg-" + suffix)
        user = User(email="pkg-" + suffix + "@example.invalid")
        db.add_all([workspace, user])
        db.flush()
        member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin")
        patient = Patient(workspace_id=workspace.id, first_name="Synthetic Patient")
        branch = Branch(workspace_id=workspace.id, name="Synthetic Clinic", code="S1")
        staff = Staff(workspace_id=workspace.id, first_name="Synthetic", last_name="Doctor")
        primary = Service(
            workspace_id=workspace.id, name="Full Body", slug="full-body",
            operational_category="laser", requires_laser_device=True,
            duration_minutes=30, price_minor=50000, currency="EGP",
        )
        face = Service(
            workspace_id=workspace.id, name="Face", slug="face",
            operational_category="laser", requires_laser_device=True,
            duration_minutes=20, price_minor=20000, currency="EGP",
        )
        device = ClinicLaserDevice(
            workspace_id=workspace.id, device_key="candela", name="Candela"
        )
        other_device = ClinicLaserDevice(
            workspace_id=workspace.id, device_key="other", name="Other"
        )
        db.add_all([member, patient, branch, staff, primary, face, device, other_device])
        db.flush()
        doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
        db.add(doctor)
        db.flush()
        db.add_all([
            DoctorService(workspace_id=workspace.id, doctor_id=doctor.id, service_id=primary.id),
            DoctorService(workspace_id=workspace.id, doctor_id=doctor.id, service_id=face.id),
            ServiceDevicePrice(
                workspace_id=workspace.id, service_id=primary.id, device_key="candela",
                device_name="Candela", duration_minutes=30, price_minor=50000, currency="EGP",
            ),
            ServiceDevicePrice(
                workspace_id=workspace.id, service_id=face.id, device_key="candela",
                device_name="Candela", duration_minutes=20, price_minor=20000, currency="EGP",
            ),
        ])
        main_pkg = PatientPackage(
            workspace_id=workspace.id, patient_id=patient.id, service_id=primary.id,
            name="Full Body Package", sessions_purchased=2, sale_price_minor=100000,
            purchased_at=purchased, laser_device_key="candela",
            laser_device_name="Candela",
        )
        face_pkg = PatientPackage(
            workspace_id=workspace.id, patient_id=patient.id, service_id=face.id,
            name="Face Package", sessions_purchased=2, sale_price_minor=40000,
            purchased_at=purchased, laser_device_key="candela", laser_device_name="Candela",
        )
        db.add_all([main_pkg, face_pkg])
        db.flush()
        appt = Appointment(
            workspace_id=workspace.id, patient_id=patient.id,
            branch_id=branch.id, doctor_id=doctor.id, service_id=primary.id,
            patient_package_id=main_pkg.id, billing_context="package_prepaid",
            laser_device_key="candela", laser_device_name="Candela",
            start_at=start, end_at=start + timedelta(minutes=30),
            busy_start_at=start, busy_end_at=start + timedelta(minutes=30),
            duration_minutes=30, price_minor=50000, currency="EGP",
            status="completed",
        )
        db.add(appt)
        db.flush()
        db.add(PackageUsage(
            workspace_id=workspace.id, patient_package_id=main_pkg.id,
            appointment_id=appt.id, sessions_used=1, status="consumed", used_at=start,
        ))
        db.commit()

    app = FastAPI()
    app.include_router(appointment_route.router, prefix="/booking")
    app.include_router(payment_route.router, prefix="/payments")
    access = WorkspaceAccess(user=user, workspace=workspace, membership=member)
    app.dependency_overrides[get_workspace_access] = lambda: access

    def override_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    # The synthetic workspace has no external integration; retain the real
    # FastAPI handler, auth scope, SQL, commerce checks, and transaction path.
    monkeypatch.setattr(appointment_route, "_require_local_write", lambda *_args: None)

    with TestClient(app) as client:
        yield {
            "client": client, "app": app, "sessions": sessions, "workspace": workspace,
            "user": user, "member": member, "patient": patient, "start": start,
            "appointment": appt, "primary": primary, "face": face,
            "main_pkg": main_pkg, "face_pkg": face_pkg, "doctor": doctor,
        }


def post(ctx, *, key="existing-face-1", package_id=None, service_id=None, device="candela", **fields):
    payload = {
        "service_id": str(service_id or ctx["face"].id),
        "laser_device_key": device,
        "pulse_mode": "none",
        "patient_package_id": str(package_id or ctx["face_pkg"].id),
    }
    payload.update(fields)
    return ctx["client"].post(
        "/booking/appointments/" + str(ctx["appointment"].id) + "/additional-services",
        json=payload, headers={"Idempotency-Key": key} if key else {},
    )


def state(ctx):
    with ctx["sessions"]() as db:
        appointment = db.get(Appointment, ctx["appointment"].id)
        face = package_read(db, db.get(PatientPackage, ctx["face_pkg"].id))
        main = package_read(db, db.get(PatientPackage, ctx["main_pkg"].id))
        lines = list(db.scalars(select(AppointmentAdditionalService).where(
            AppointmentAdditionalService.appointment_id == appointment.id,
        )).all())
        usages = list(db.scalars(select(PackageUsage).where(
            PackageUsage.appointment_id == appointment.id,
        )).all())
        tx_count = db.scalar(select(func.count(PaymentTransaction.id)).where(
            PaymentTransaction.workspace_id == ctx["workspace"].id,
        ))
        return {
            "appt_status": appointment.status, "price": appointment.price_minor,
            "face_remaining": face.sessions_remaining,
            "main_remaining": main.sessions_remaining,
            "lines": [(str(x.id), str(x.patient_package_id), x.billing_context) for x in lines],
            "usages": [(str(x.patient_package_id), x.status, str(x.appointment_additional_service_id)) for x in usages],
            "transactions": tx_count,
        }


def summary(ctx):
    r = ctx["client"].get(
        "/payments/appointments/" + str(ctx["appointment"].id)
    )
    assert r.status_code == 200, r.text
    return r.json()


def assert_no_new_usage(ctx, *, expected_face_remaining=2):
    s = state(ctx)
    assert s["face_remaining"] == expected_face_remaining
    assert s["main_remaining"] == 1
    assert len(s["lines"]) == 0
    assert len(s["usages"]) == 1
    assert s["transactions"] == 0
    assert s["price"] == 50000


def test_completed_two_existing_packages_consume_face_once_without_charge(ctx):
    before = summary(ctx)
    assert before["price_minor"] == 0
    r = post(ctx)
    assert r.status_code == 201, r.text
    line = r.json()
    assert line["service_id"] == str(ctx["face"].id)
    assert line["patient_package_id"] == str(ctx["face_pkg"].id)
    assert line["billing_context"] == "package_prepaid"
    s = state(ctx)
    assert (s["face_remaining"], s["main_remaining"]) == (1, 1)
    assert s["appt_status"] == "completed"
    assert len(s["lines"]) == 1 and len(s["usages"]) == 2
    assert len([u for u in s["usages"] if u[1] == "consumed"]) == 2
    assert s["price"] == 50000 and s["transactions"] == 0
    due = summary(ctx)
    assert due["price_minor"] == 0 and due["additional_services_total_minor"] == 0
    assert due["balance_minor"] == 0


def test_completed_same_key_replay_returns_existing_line_no_second_usage(ctx):
    first = post(ctx, key="replay-1")
    second = post(ctx, key="replay-1")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    s = state(ctx)
    assert s["face_remaining"] == 1 and len(s["usages"]) == 2
    assert len(s["lines"]) == 1


def test_same_key_different_package_rejected_without_extra_usage(ctx):
    r = post(ctx, key="same-key-different")
    assert r.status_code == 201, r.text
    with ctx["sessions"]() as db:
        pkg = PatientPackage(
            workspace_id=ctx["workspace"].id, patient_id=ctx["patient"].id,
            service_id=ctx["face"].id, name="Face Spare Package", sessions_purchased=2,
            sale_price_minor=40000, purchased_at=ctx["start"] - timedelta(days=5),
            laser_device_key="candela", laser_device_name="Candela",
        )
        db.add(pkg)
        db.commit()
    replay = post(ctx, key="same-key-different", package_id=pkg.id)
    assert replay.status_code == 409, replay.text
    assert state(ctx)["face_remaining"] == 1


def test_new_key_cannot_duplicate_additional_service(ctx):
    assert post(ctx, key="first").status_code == 201
    replay = post(ctx, key="second")
    assert replay.status_code == 409
    assert state(ctx)["face_remaining"] == 1


@pytest.mark.parametrize("status", ["pending", "confirmed", "checked_in", "in_progress"])
def test_incomplete_appointment_reserves_then_consumes_once(ctx, status):
    with ctx["sessions"]() as db:
        db.get(Appointment, ctx["appointment"].id).status = status
        db.commit()
    r = post(ctx)
    assert r.status_code == 201, r.text
    s = state(ctx)
    assert s["face_remaining"] == 1
    face_use = [row for row in s["usages"] if row[0] == str(ctx["face_pkg"].id)]
    assert len(face_use) == 1 and face_use[0][1] == "reserved"
    with ctx["sessions"]() as db:
        appt = db.get(Appointment, ctx["appointment"].id)
        appt.status = "completed"
        consume_visit_package_usages(db, appointment=appt)
        consume_visit_package_usages(db, appointment=appt)
        db.commit()
    final = state(ctx)
    assert len(final["usages"]) == 2
    assert final["face_remaining"] == 1
    assert all(u[1] == "consumed" for u in final["usages"])


@pytest.mark.parametrize(
    "condition",
    ["other_patient", "wrong_service", "wrong_device", "expired", "cancelled", "exhausted", "after_appointment"],
)
def test_invalid_entitlements_rejected_without_partial_mutation(ctx, condition):
    with ctx["sessions"]() as db:
        pkg = db.get(PatientPackage, ctx["face_pkg"].id)
        if condition == "other_patient":
            stranger = Patient(workspace_id=ctx["workspace"].id, first_name="Synthetic Stranger")
            db.add(stranger)
            db.flush()
            pkg.patient_id = stranger.id
        elif condition == "wrong_service":
            pkg.service_id = ctx["primary"].id
        elif condition == "wrong_device":
            pkg.laser_device_key = "other"
        elif condition == "expired":
            pkg.expires_at = date(2026, 11, 9)
        elif condition == "cancelled":
            pkg.status = "cancelled"
        elif condition == "exhausted":
            pkg.opening_sessions_remaining = 0
        elif condition == "after_appointment":
            pkg.purchased_at = ctx["start"] + timedelta(days=1)
        db.commit()
    response = post(ctx)
    assert response.status_code == 409, response.text
    assert_no_new_usage(
        ctx, expected_face_remaining=0 if condition == "exhausted" else 2
    )


def test_cross_workspace_package_not_visible_or_consumed(ctx):
    with ctx["sessions"]() as db:
        other_workspace = Workspace(
            name="Other synthetic workspace", slug="other-pkg-" + uuid4().hex[:12]
        )
        db.add(other_workspace)
        db.flush()
        other_patient = Patient(
            workspace_id=other_workspace.id, first_name="Other synthetic patient"
        )
        other_service = Service(
            workspace_id=other_workspace.id, name="Other Face", slug="face-other",
            operational_category="dermatology", requires_laser_device=False,
            duration_minutes=20, price_minor=10000,
        )
        db.add_all([other_patient, other_service])
        db.flush()
        foreign = PatientPackage(
            workspace_id=other_workspace.id, patient_id=other_patient.id,
            service_id=other_service.id, name="Foreign Package",
            sessions_purchased=3, sale_price_minor=30000,
            purchased_at=ctx["start"] - timedelta(days=2),
        )
        db.add(foreign)
        db.commit()
    response = post(ctx, package_id=foreign.id)
    assert response.status_code == 409
    assert_no_new_usage(ctx)


def test_duplicate_primary_service_rejected(ctx):
    response = post(ctx, service_id=ctx["primary"].id)
    assert response.status_code == 409
    assert_no_new_usage(ctx)


def test_doctor_without_service_assignment_rejected(ctx):
    with ctx["sessions"]() as db:
        assignment = db.scalar(select(DoctorService).where(
            DoctorService.workspace_id == ctx["workspace"].id,
            DoctorService.doctor_id == ctx["doctor"].id,
            DoctorService.service_id == ctx["face"].id,
        ))
        assignment.is_active = False
        db.commit()
    assert post(ctx).status_code == 409
    assert_no_new_usage(ctx)


def test_mismatched_operational_category_rejected(ctx):
    with ctx["sessions"]() as db:
        service = db.get(Service, ctx["face"].id)
        service.operational_category = "dermatology"
        service.requires_laser_device = False
        db.commit()
    assert post(ctx).status_code == 409
    assert_no_new_usage(ctx)


def test_unavailable_expansion_rolls_back(ctx, monkeypatch):
    with ctx["sessions"]() as db:
        appt = db.get(Appointment, ctx["appointment"].id)
        appt.status = "confirmed"
        price = db.scalar(select(ServiceDevicePrice).where(
            ServiceDevicePrice.workspace_id == ctx["workspace"].id,
            ServiceDevicePrice.service_id == ctx["face"].id,
        ))
        price.duration_minutes = 40
        db.commit()

    def blocked_expansion(*_args, **_kwargs):
        raise commerce.AppointmentCommerceError("Synthetic expansion unavailable")

    monkeypatch.setattr(commerce, "_ensure_duration_expansion_available", blocked_expansion)
    result = post(ctx)
    assert result.status_code == 409
    assert_no_new_usage(ctx)


def test_snapshot_error_rolls_back_line_and_entitlement(ctx, monkeypatch):
    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("synthetic snapshot failure")

    monkeypatch.setattr(commerce, "refresh_appointment_payment_snapshots", fail_snapshot)
    with pytest.raises(RuntimeError, match="synthetic snapshot failure"):
        post(ctx)
    assert_no_new_usage(ctx)


def test_additional_service_standard_pricing_path_unchanged(ctx):
    result = post(ctx, package_id=None, key="standard-path", patient_package_id=None)
    assert result.status_code == 201, result.text
    due = summary(ctx)
    assert due["additional_services_total_minor"] == 20000
    assert due["price_minor"] == 20000
    assert state(ctx)["face_remaining"] == 2


def test_pulse_and_package_combination_rejected_without_mutation(ctx):
    r = post(ctx, pulse_mode="use_balance", pulses_used=20)
    assert r.status_code == 422
    assert_no_new_usage(ctx)

def test_existing_product_and_payment_remain_unchanged(ctx):
    with ctx["sessions"]() as db:
        product = ClinicProduct(
            workspace_id=ctx["workspace"].id,
            name="Synthetic Product",
            quantity_on_hand=12,
        )
        db.add(product)
        db.flush()
        db.add(AppointmentProductLine(
            workspace_id=ctx["workspace"].id,
            appointment_id=ctx["appointment"].id,
            product_id=product.id,
            product_name=product.name,
            quantity=1,
            unit_price_minor=10000,
            currency="EGP",
        ))
        payment = PaymentTransaction(
            workspace_id=ctx["workspace"].id,
            appointment_id=ctx["appointment"].id,
            patient_id=ctx["patient"].id,
            transaction_type="payment",
            amount_minor=5000,
            currency="EGP",
            payment_method="cash",
            source="staff",
        )
        db.add(payment)
        db.flush()
        db.add(PaymentAllocation(
            workspace_id=ctx["workspace"].id,
            appointment_id=ctx["appointment"].id,
            transaction_id=payment.id,
            amount_minor=5000,
        ))
        db.commit()
    before = summary(ctx)
    assert (before["price_minor"], before["balance_minor"]) == (10000, 5000)
    response = post(ctx)
    assert response.status_code == 201, response.text
    after = summary(ctx)
    assert (after["price_minor"], after["balance_minor"]) == (10000, 5000)
    assert after["products_total_minor"] == 10000
    assert after["additional_services_total_minor"] == 0
    assert state(ctx)["transactions"] == 1
    with ctx["sessions"]() as db:
        assert db.scalar(select(func.count(AppointmentProductLine.id)).where(
            AppointmentProductLine.appointment_id == ctx["appointment"].id,
        )) == 1

def _another_authorized_service(ctx):
    """A genuinely different, doctor-authorized laser service on this visit."""
    with ctx["sessions"]() as db:
        other = Service(
            workspace_id=ctx["workspace"].id,
            name="Arms", slug="synthetic-arms",
            operational_category="laser", requires_laser_device=True,
            duration_minutes=15, price_minor=8000, currency="EGP",
        )
        db.add(other)
        db.flush()
        db.add_all([
            DoctorService(
                workspace_id=ctx["workspace"].id,
                doctor_id=ctx["doctor"].id, service_id=other.id,
            ),
            ServiceDevicePrice(
                workspace_id=ctx["workspace"].id,
                service_id=other.id, device_key="candela",
                device_name="Candela", duration_minutes=15,
                price_minor=8000, currency="EGP",
            ),
        ])
        db.commit()
        return other.id


def _assert_rejected_without_side_effects(ctx, response, before):
    assert response.status_code == 409, response.text
    assert "Idempotency key was already used" in response.text
    assert state(ctx) == before["state"]
    after_summary = summary(ctx)
    assert (
        after_summary["price_minor"],
        after_summary["balance_minor"],
        after_summary["additional_services_total_minor"],
        after_summary["pulse_overage_total_minor"],
    ) == before["financial"]


def _financial_snapshot(ctx):
    charge = summary(ctx)
    return {
        "state": state(ctx),
        "financial": (
            charge["price_minor"],
            charge["balance_minor"],
            charge["additional_services_total_minor"],
            charge["pulse_overage_total_minor"],
        ),
    }


@pytest.mark.parametrize("second_intent", ["standard", "pulse"])
def test_f01_package_first_rejects_same_key_other_service_intent(ctx, second_intent):
    other_service_id = _another_authorized_service(ctx)
    key = "f01-package-first-" + second_intent
    original = post(ctx, key=key)
    assert original.status_code == 201, original.text
    before = _financial_snapshot(ctx)
    replacement = (
        {"patient_package_id": None, "pulse_mode": "none"}
        if second_intent == "standard"
        else {"patient_package_id": None, "pulse_mode": "overage", "pulses_used": 10}
    )
    attempted = post(
        ctx, key=key, service_id=other_service_id, **replacement
    )
    _assert_rejected_without_side_effects(ctx, attempted, before)


def test_f01_standard_first_rejects_package_with_same_key(ctx):
    other_service_id = _another_authorized_service(ctx)
    key = "f01-standard-first"
    standard = post(
        ctx, key=key, service_id=other_service_id, patient_package_id=None,
    )
    assert standard.status_code == 201, standard.text
    before = _financial_snapshot(ctx)
    assert before["financial"][0] == 8000
    attempted = post(ctx, key=key)
    _assert_rejected_without_side_effects(ctx, attempted, before)
    assert state(ctx)["face_remaining"] == 2


def test_f01_standard_same_key_replays_its_line_and_does_not_double_charge(ctx):
    other_service_id = _another_authorized_service(ctx)
    first = post(
        ctx, key="f01-standard-replay", service_id=other_service_id,
        patient_package_id=None,
    )
    assert first.status_code == 201, first.text
    before = _financial_snapshot(ctx)
    again = post(
        ctx, key="f01-standard-replay", service_id=other_service_id,
        patient_package_id=None,
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert _financial_snapshot(ctx) == before


def _configure_synthetic_pulse_overage(ctx):
    with ctx["sessions"]() as db:
        db.add(PulseBillingSettings(
            workspace_id=ctx["workspace"].id, device_key="candela",
            overage_price_minor=100, currency="EGP",
        ))
        db.commit()


def test_f01_pulse_first_rejects_package_with_same_key(ctx):
    other_service_id = _another_authorized_service(ctx)
    _configure_synthetic_pulse_overage(ctx)
    key = "f01-pulse-first"
    pulse = post(
        ctx, key=key, service_id=other_service_id,
        patient_package_id=None, pulse_mode="overage", pulses_used=10,
    )
    assert pulse.status_code == 201, pulse.text
    assert pulse.json()["billing_context"] == "pulse_prepaid"
    before = _financial_snapshot(ctx)
    assert before["financial"][0] == 1000
    attempted = post(ctx, key=key)
    _assert_rejected_without_side_effects(ctx, attempted, before)
    assert state(ctx)["face_remaining"] == 2


def test_f01_pulse_replay_and_changed_pulse_quantity(ctx):
    other_service_id = _another_authorized_service(ctx)
    _configure_synthetic_pulse_overage(ctx)
    request = {
        "key": "f01-pulse-replay", "service_id": other_service_id,
        "patient_package_id": None, "pulse_mode": "overage", "pulses_used": 8,
    }
    first = post(ctx, **request)
    assert first.status_code == 201, first.text
    before = _financial_snapshot(ctx)
    identical = post(ctx, **request)
    assert identical.status_code == 201, identical.text
    assert identical.json()["id"] == first.json()["id"]
    assert _financial_snapshot(ctx) == before
    different = post(ctx, **(request | {"pulses_used": 9}))
    _assert_rejected_without_side_effects(ctx, different, before)


def test_f01_different_valid_keys_allow_genuinely_distinct_services(ctx):
    other_service_id = _another_authorized_service(ctx)
    first = post(ctx, key="f01-first-valid-service")
    assert first.status_code == 201, first.text
    second = post(
        ctx, key="f01-second-valid-service",
        service_id=other_service_id, patient_package_id=None,
    )
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    current = state(ctx)
    assert current["face_remaining"] == 1
    assert current["main_remaining"] == 1
    assert current["price"] == 50000
    assert current["transactions"] == 0
    assert len(current["lines"]) == 2
    assert len(current["usages"]) == 2
    financial = summary(ctx)
    assert financial["price_minor"] == 8000
    assert financial["additional_services_total_minor"] == 8000
    assert financial["balance_minor"] == 8000
