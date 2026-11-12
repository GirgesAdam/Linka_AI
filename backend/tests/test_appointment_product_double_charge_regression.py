"""Real API -> PostgreSQL 17.6 -> payment summary product-billing regression.

Requires a *disposable*, local-only database explicitly supplied as
BILLING_TEST_DATABASE_URL. Never connects to Production or Staging.
No Alembic migrations are run; the fixture creates only required test tables.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes import inventory as inventory_route
from app.api.routes import payments as payments_route
from app.database.base import Base
from app.database.session import get_db
from app.models import (
    Appointment,
    AppointmentProductLine,
    Branch,
    ClinicProduct,
    Doctor,
    Patient,
    PaymentAllocation,
    PaymentTransaction,
    Service,
    Staff,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services.finance import outstanding_balances


@pytest.fixture(scope="module")
def local_pg_engine():
    raw = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not raw:
        pytest.skip("Requires explicit disposable BILLING_TEST_DATABASE_URL")
    url = make_url(raw)
    assert url.host in {"127.0.0.1", "localhost"}
    assert url.database == "billing_ci"
    assert url.port == 55431

    engine = create_engine(raw, pool_pre_ping=True)
    # The closure is limited to foreign-key dependencies of the actual
    # payment/appointment routes, rather than creating the whole app schema.
    names = {
        "appointments", "appointment_product_lines", "clinic_products",
        "users", "workspaces", "workspace_members", "patients",
        "payment_transactions", "payment_allocations",
        "appointment_additional_services", "patient_packages",
        "patient_pulse_packs", "appointment_pulse_settlements",
    }
    while True:
        before = len(names)
        for name in tuple(names):
            names.update(fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys)
        if len(names) == before:
            break
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in names])
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def billing_env(local_pg_engine, monkeypatch):
    session_factory = sessionmaker(bind=local_pg_engine, autoflush=False, expire_on_commit=False)
    with session_factory() as db:
        suffix = uuid4().hex[:12]
        workspace = Workspace(name="Isolated Billing Fixture", slug=f"billing-{suffix}")
        user = User(email=f"billing-{suffix}@example.invalid")
        db.add_all([workspace, user])
        db.flush()

        membership = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin")
        branch = Branch(workspace_id=workspace.id, name="Test Branch", code="B1")
        staff = Staff(workspace_id=workspace.id, first_name="Test", last_name="Doctor")
        service = Service(
            workspace_id=workspace.id, name="Primary Service", slug="primary",
            duration_minutes=30, price_minor=50000,
        )
        patient = Patient(workspace_id=workspace.id, first_name="Test Patient")
        db.add_all([membership, branch, staff, service, patient])
        db.flush()
        doctor = Doctor(workspace_id=workspace.id, staff_id=staff.id)
        db.add(doctor)
        db.flush()
        start = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
        appointment = Appointment(
            workspace_id=workspace.id, patient_id=patient.id,
            branch_id=branch.id, doctor_id=doctor.id, service_id=service.id,
            start_at=start, end_at=start + timedelta(minutes=30),
            busy_start_at=start, busy_end_at=start + timedelta(minutes=30),
            duration_minutes=30, price_minor=50000, status="completed",
            currency="EGP", billing_context="standard",
        )
        product_a = ClinicProduct(
            workspace_id=workspace.id, name="Product A", quantity_on_hand=20
        )
        product_b = ClinicProduct(
            workspace_id=workspace.id, name="Product B", quantity_on_hand=20
        )
        db.add_all([appointment, product_a, product_b])
        db.commit()

    app = FastAPI()
    app.include_router(inventory_route.router, prefix="/inventory")
    app.include_router(payments_route.router, prefix="/payments")
    access = WorkspaceAccess(user=user, workspace=workspace, membership=membership)
    app.dependency_overrides[get_workspace_access] = lambda: access

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(payments_route, "_can_refund_appointment", lambda _access: False)

    with TestClient(app) as client:
        yield {
            "client": client, "app": app, "session_factory": session_factory,
            "workspace": workspace, "user": user, "membership": membership,
            "appointment": appointment, "patient": patient,
            "product_a": product_a, "product_b": product_b,
        }


def _summary(env):
    r = env["client"].get(f"/payments/appointments/{env['appointment'].id}")
    assert r.status_code == 200, r.text
    return r.json()


def _add(env, *, quantity=1, price_minor=10000, product="product_a"):
    r = env["client"].post(
        f"/inventory/appointments/{env['appointment'].id}/products",
        json={
            "product_id": str(env[product].id),
            "quantity": quantity,
            "unit_price_minor": price_minor,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _delete(env, line_id):
    r = env["client"].delete(
        f"/inventory/appointments/{env['appointment'].id}/products/{line_id}",
    )
    assert r.status_code == 204, r.text


def _db_state(env):
    with env["session_factory"]() as db:
        a = db.get(Appointment, env["appointment"].id)
        stock = {
            key: db.get(ClinicProduct, env[key].id).quantity_on_hand
            for key in ("product_a", "product_b")
        }
        count = db.scalar(
            select(func.count(AppointmentProductLine.id))
            .where(AppointmentProductLine.workspace_id == env["workspace"].id,
                   AppointmentProductLine.appointment_id == a.id)
        )
        return (a.price_minor, a.payment_status, a.amount_paid_minor, stock, count)


def _pay(env, amount):
    with env["session_factory"]() as db:
        a = env["appointment"]
        tx = PaymentTransaction(
            workspace_id=env["workspace"].id, appointment_id=a.id,
            patient_id=a.patient_id, transaction_type="payment",
            amount_minor=amount, currency="EGP", payment_method="cash",
            source="staff",
        )
        db.add(tx)
        db.flush()
        db.add(PaymentAllocation(
            workspace_id=env["workspace"].id, appointment_id=a.id,
            transaction_id=tx.id, amount_minor=amount,
        ))
        db.commit()


def test_one_product_counted_once_and_base_price_unchanged(billing_env):
    assert _summary(billing_env)["price_minor"] == 50000
    lines = _add(billing_env)
    assert len(lines) == 1
    s = _summary(billing_env)
    assert (s["service_price_minor"], s["products_total_minor"],
            s["subtotal_minor"], s["price_minor"], s["balance_minor"]) == (
            50000, 10000, 60000, 60000, 60000)
    assert _db_state(billing_env) == (50000, "unpaid", 0, {"product_a": 19, "product_b": 20}, 1)


@pytest.mark.parametrize("quantity, expected_product_due", [(2, 20000), (3, 30000)])
def test_quantity_is_multiplied_only_once(billing_env, quantity, expected_product_due):
    lines = _add(billing_env, quantity=quantity)
    assert lines[0]["total_minor"] == expected_product_due
    s = _summary(billing_env)
    assert s["products_total_minor"] == expected_product_due
    assert s["price_minor"] == 50000 + expected_product_due
    assert _db_state(billing_env)[0] == 50000


def test_different_products_add_without_double_charge(billing_env):
    _add(billing_env, quantity=2, price_minor=10000)
    _add(billing_env, quantity=3, price_minor=5000, product="product_b")
    s = _summary(billing_env)
    assert (s["products_total_minor"], s["price_minor"]) == (35000, 85000)
    assert _db_state(billing_env)[3] == {"product_a": 18, "product_b": 17}


def test_same_product_second_intentional_sale_is_separate_line(billing_env):
    _add(billing_env)
    lines = _add(billing_env)
    assert len(lines) == 2
    assert len({line["id"] for line in lines}) == 2
    assert (_summary(billing_env)["products_total_minor"],
            _summary(billing_env)["price_minor"]) == (20000, 70000)
    assert _db_state(billing_env)[3]["product_a"] == 18


def test_delete_one_line_then_last_line_preserves_primary_service(billing_env):
    lines = _add(billing_env, quantity=2)
    first_id = lines[0]["id"]
    lines = _add(billing_env, price_minor=5000, product="product_b")
    second_id = next(x["id"] for x in lines if x["id"] != first_id)
    assert _summary(billing_env)["price_minor"] == 75000
    _delete(billing_env, first_id)
    assert _summary(billing_env)["price_minor"] == 55000
    assert _db_state(billing_env)[3]["product_a"] == 20
    _delete(billing_env, second_id)
    assert (_summary(billing_env)["products_total_minor"],
            _summary(billing_env)["price_minor"]) == (0, 50000)
    assert _db_state(billing_env) == (50000, "unpaid", 0, {"product_a": 20, "product_b": 20}, 0)


def test_discount_applied_after_product_sum(billing_env):
    with billing_env["session_factory"]() as db:
        db.get(Appointment, billing_env["appointment"].id).discount_minor = 7000
        db.commit()
    _add(billing_env, quantity=2)
    s = _summary(billing_env)
    assert (s["subtotal_minor"], s["discount_minor"], s["price_minor"]) == (70000, 7000, 63000)
    assert _db_state(billing_env)[0] == 50000


def test_partial_payment_balance_and_snapshot(billing_env):
    _pay(billing_env, 30000)
    _add(billing_env, quantity=2)
    s = _summary(billing_env)
    assert (s["net_paid_minor"], s["balance_minor"], s["payment_status"],
            s["price_minor"]) == (30000, 40000, "partial", 70000)
    assert _db_state(billing_env)[:3] == (50000, "partial", 30000)


def test_fully_paid_then_product_added_creates_only_product_balance(billing_env):
    _pay(billing_env, 50000)
    assert _summary(billing_env)["payment_status"] == "paid"
    _add(billing_env, quantity=2)
    s = _summary(billing_env)
    assert (s["net_paid_minor"], s["balance_minor"], s["payment_status"]) == (50000, 20000, "partial")
    assert _db_state(billing_env)[:3] == (50000, "partial", 50000)


@pytest.mark.parametrize("billing_context", ["package_prepaid", "pulse_prepaid"])
def test_prepaid_session_charges_only_product(billing_env, billing_context):
    with billing_env["session_factory"]() as db:
        db.get(Appointment, billing_env["appointment"].id).billing_context = billing_context
        db.commit()
    _add(billing_env, quantity=2)
    s = _summary(billing_env)
    assert (s["service_price_minor"], s["products_total_minor"],
            s["price_minor"], s["balance_minor"]) == (50000, 20000, 20000, 20000)
    assert _db_state(billing_env)[0] == 50000


def test_completed_appointment_finance_balance_uses_single_product_total(billing_env):
    _add(billing_env, quantity=2)
    with billing_env["session_factory"]() as db:
        result = outstanding_balances(db, workspace_id=billing_env["workspace"].id)
    assert result.rows[0].balance_minor == 70000
    assert result.rows[0].appointment_count == 1


def test_out_of_stock_rejected_without_mutation(billing_env):
    response = billing_env["client"].post(
        f"/inventory/appointments/{billing_env['appointment'].id}/products",
        json={
            "product_id": str(billing_env["product_a"].id),
            "quantity": 21,
            "unit_price_minor": 10000,
        },
    )
    assert response.status_code == 409
    assert _db_state(billing_env) == (50000, "unknown", None, {"product_a": 20, "product_b": 20}, 0)


def test_snapshot_failure_rolls_back_product_and_stock(billing_env, monkeypatch):
    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("isolated snapshot failure")

    monkeypatch.setattr(inventory_route, "refresh_appointment_payment_snapshots", fail_snapshot)
    with pytest.raises(RuntimeError, match="isolated snapshot failure"):
        billing_env["client"].post(
            f"/inventory/appointments/{billing_env['appointment'].id}/products",
            json={
                "product_id": str(billing_env["product_a"].id),
                "quantity": 2,
                "unit_price_minor": 10000,
            },
        )
    assert _db_state(billing_env) == (50000, "unknown", None, {"product_a": 20, "product_b": 20}, 0)


def test_cross_workspace_add_rejected_without_mutation(billing_env):
    bogus_workspace = Workspace(name="Unauthorized", slug=f"unauth-{uuid4().hex[:12]}")
    billing_env["app"].dependency_overrides[get_workspace_access] = lambda: WorkspaceAccess(
        user=billing_env["user"], workspace=bogus_workspace,
        membership=billing_env["membership"],
    )
    try:
        response = billing_env["client"].post(
            f"/inventory/appointments/{billing_env['appointment'].id}/products",
            json={
                "product_id": str(billing_env["product_a"].id),
                "quantity": 2,
                "unit_price_minor": 10000,
            },
        )
        assert response.status_code == 404
        summary = billing_env["client"].get(
            f"/payments/appointments/{billing_env['appointment'].id}"
        )
        assert summary.status_code == 404
    finally:
        billing_env["app"].dependency_overrides[get_workspace_access] = lambda: WorkspaceAccess(
            user=billing_env["user"], workspace=billing_env["workspace"],
            membership=billing_env["membership"],
        )
    assert _db_state(billing_env) == (50000, "unknown", None, {"product_a": 20, "product_b": 20}, 0)


def test_legacy_inflated_price_is_not_silently_rewritten(billing_env):
    # Simulates a polluted historical record *only inside disposable test DB*.
    with billing_env["session_factory"]() as db:
        db.get(Appointment, billing_env["appointment"].id).price_minor = 70000
        db.add(AppointmentProductLine(
            workspace_id=billing_env["workspace"].id,
            appointment_id=billing_env["appointment"].id,
            product_id=billing_env["product_a"].id,
            product_name="Product A (historical)", quantity=2,
            unit_price_minor=10000, currency="EGP",
        ))
        db.commit()
    assert _summary(billing_env)["price_minor"] == 90000
    lines = _add(billing_env, price_minor=5000, product="product_b")
    assert _summary(billing_env)["price_minor"] == 95000
    _delete(billing_env, next(x["id"] for x in lines if x["product_id"] == str(billing_env["product_b"].id)))
    assert _summary(billing_env)["price_minor"] == 90000
    assert _db_state(billing_env)[0] == 70000
