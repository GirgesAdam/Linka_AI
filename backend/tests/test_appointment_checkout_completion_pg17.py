"""Real API checkout/completion regressions on disposable localhost PostgreSQL 17.6."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_existing_package_additional_service_pg17 import (
    ctx as ctx,
)
from test_existing_package_additional_service_pg17 import (
    engine as engine,
)
from test_existing_package_additional_service_pg17 import (
    post as add_additional,
)
from test_existing_package_additional_service_pg17 import (
    summary,
)

from app.database.base import Base
from app.database.session import get_db
from app.models import (
    ActivityEvent,
    Appointment,
    AppointmentStatusHistory,
    AutomationJob,
    AutomationRule,
    PackageUsage,
    PaymentTransaction,
)
from app.services import appointment_checkout as checkout_service
from app.services import appointment_operations
from app.services.appointment_operations import AppointmentOperationError
from app.services.patient_packages import PackageOperationError, package_read
from app.services.payments import PaymentOperationError


@pytest.fixture(scope="module", autouse=True)
def checkout_tables(engine):
    """Install only extra fixture tables on the disposable package test DB."""
    names = {"appointment_status_history", "automation_jobs", "clinic_integrations"}
    while True:
        count = len(names)
        for name in tuple(names):
            names.update(fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys)
        if len(names) == count:
            break
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[name] for name in names]
    )


@pytest.fixture()
def visit(ctx, checkout_tables):
    """Turn a synthetic completed fixture into a past, confirmed, reserved visit."""
    with ctx["sessions"]() as db:
        appointment = db.get(Appointment, ctx["appointment"].id)
        appointment.status = "confirmed"
        appointment.completed_at = None
        usage = db.scalar(select(PackageUsage).where(
            PackageUsage.workspace_id == ctx["workspace"].id,
            PackageUsage.appointment_id == appointment.id,
        ))
        assert usage is not None
        usage.status = "reserved"
        usage.used_at = None
        db.commit()
    return ctx


def checkout(ctx, *, amount=0, discount=0, key="checkout-1", **updates):
    body = {
        "amount_minor": amount,
        "payment_method": "cash",
        "discount_minor": discount,
        "pulse_mode": "none",
    } | updates
    return ctx["client"].post(
        "/payments/appointments/" + str(ctx["appointment"].id) + "/checkout",
        json=body,
        headers={"Idempotency-Key": key} if key else {},
    )


def make_standard(ctx):
    with ctx["sessions"]() as db:
        appointment = db.get(Appointment, ctx["appointment"].id)
        appointment.patient_package_id = None
        appointment.billing_context = "standard"
        for usage in db.scalars(select(PackageUsage).where(
            PackageUsage.appointment_id == appointment.id,
        )):
            db.delete(usage)
        db.commit()


def snapshot(ctx):
    with ctx["sessions"]() as db:
        appointment = db.get(Appointment, ctx["appointment"].id)
        usages = list(db.scalars(select(PackageUsage).where(
            PackageUsage.appointment_id == appointment.id,
        )))
        return {
            "status": appointment.status,
            "completed_at": appointment.completed_at,
            "payments": db.scalar(select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.appointment_id == appointment.id,
            )),
            "history": db.scalar(select(func.count(AppointmentStatusHistory.id)).where(
                AppointmentStatusHistory.appointment_id == appointment.id,
            )),
            "checkout_events": db.scalar(select(func.count(ActivityEvent.id)).where(
                ActivityEvent.action == "appointment.checkout",
                ActivityEvent.entity_id == appointment.id,
            )),
            "usage": tuple(sorted((str(u.id), u.status) for u in usages)),
        }


def test_zero_balance_checkout_completes_without_fictional_payment(visit):
    before = summary(visit)
    assert before["balance_minor"] == 0
    response = checkout(visit)
    assert response.status_code == 200, response.text
    result = snapshot(visit)
    assert result["status"] == "completed"
    assert result["completed_at"] is not None
    assert result["payments"] == 0
    assert result["history"] == 1
    assert result["checkout_events"] == 1
    assert result["usage"][0][1] == "consumed"


def test_checkout_two_existing_packages_consumes_each_once(visit):
    extra = add_additional(visit)
    assert extra.status_code == 201, extra.text
    response = checkout(visit, key="two-entitlements")
    assert response.status_code == 200, response.text
    with visit["sessions"]() as db:
        primary = package_read(db, db.get(type(visit["main_pkg"]), visit["main_pkg"].id))
        face = package_read(db, db.get(type(visit["face_pkg"]), visit["face_pkg"].id))
        assert primary.sessions_remaining == 1
        assert face.sessions_remaining == 1
        assert primary.sessions_consumed == 1 and face.sessions_consumed == 1
    assert snapshot(visit)["payments"] == 0
    assert len(snapshot(visit)["usage"]) == 2
    assert all(status == "consumed" for _, status in snapshot(visit)["usage"])


def test_same_checkout_replay_creates_no_extra_history_payment_or_usage(visit):
    assert checkout(visit, key="zero-replay").status_code == 200
    before = snapshot(visit)
    replay = checkout(visit, key="zero-replay")
    assert replay.status_code == 200
    assert snapshot(visit) == before


def test_same_checkout_key_with_different_intent_rejected(visit):
    assert checkout(visit, key="intent-locked").status_code == 200
    before = snapshot(visit)
    rejected = checkout(visit, key="intent-locked", discount=100)
    assert rejected.status_code == 409, rejected.text
    assert snapshot(visit) == before


@pytest.mark.parametrize("paid", [50000, 20000])
def test_paid_or_partially_paid_checkout_completes_with_correct_balance(visit, paid):
    make_standard(visit)
    response = checkout(visit, amount=paid)
    assert response.status_code == 200, response.text
    assert response.json()["balance_minor"] == 50000 - paid
    assert response.json()["payment_status"] != "paid" if paid < 50000 else response.json()["balance_minor"] == 0
    result = snapshot(visit)
    assert result["status"] == "completed"
    assert result["payments"] == 1
    assert result["history"] == 1
    assert result["usage"] == ()


def test_completed_visit_can_receive_later_payment_without_recompletion(visit):
    make_standard(visit)
    assert checkout(visit, amount=10000, key="first-payment").status_code == 200
    initial = snapshot(visit)
    later = checkout(visit, amount=10000, key="later-payment")
    assert later.status_code == 200, later.text
    assert later.json()["balance_minor"] == 30000
    result = snapshot(visit)
    assert result["status"] == "completed"
    assert result["history"] == 1
    assert result["payments"] == 2
    assert result["checkout_events"] == 2
    assert result["completed_at"] == initial["completed_at"]
    assert checkout(visit, amount=10000, key="later-payment").status_code == 200
    assert snapshot(visit) == result


def test_future_zero_checkout_rejected_and_prepaid_checkout_remains_pending(visit):
    make_standard(visit)
    with visit["sessions"]() as db:
        appointment = db.get(Appointment, visit["appointment"].id)
        future = datetime.now(UTC) + timedelta(days=3)
        appointment.start_at = future
        appointment.end_at = future + timedelta(minutes=30)
        appointment.busy_start_at = future
        appointment.busy_end_at = future + timedelta(minutes=30)
        db.commit()
    no_charge = checkout(visit, key="future-zero")
    assert no_charge.status_code == 409
    assert snapshot(visit)["history"] == 0
    prepay = checkout(visit, amount=10000, key="future-prepay")
    assert prepay.status_code == 200, prepay.text
    result = snapshot(visit)
    assert result["status"] == "confirmed"
    assert result["history"] == 0
    assert result["completed_at"] is None
    assert result["payments"] == 1
    assert prepay.json()["balance_minor"] == 40000


@pytest.mark.parametrize("status", ["cancelled", "no_show", "rescheduled"])
def test_invalid_status_rejected_before_payment_or_completion(visit, status):
    make_standard(visit)
    with visit["sessions"]() as db:
        db.get(Appointment, visit["appointment"].id).status = status
        db.commit()
    before = snapshot(visit)
    response = checkout(visit, amount=10000)
    assert response.status_code == 409
    assert snapshot(visit) == before


def test_zero_charge_when_balance_outstanding_rejected_without_completion(visit):
    make_standard(visit)
    before = snapshot(visit)
    response = checkout(visit)
    assert response.status_code == 409
    assert snapshot(visit) == before


def test_unsettled_pulse_checkout_cannot_complete(visit):
    with visit["sessions"]() as db:
        appointment = db.get(Appointment, visit["appointment"].id)
        appointment.patient_package_id = None
        appointment.billing_context = "pulse_prepaid"
        db.commit()
    before = snapshot(visit)
    result = checkout(visit)
    assert result.status_code == 409
    assert snapshot(visit) == before


def test_payment_failure_rolls_back(visit, monkeypatch):
    make_standard(visit)
    before = snapshot(visit)

    def fail_payment(*args, **kwargs):
        raise PaymentOperationError("synthetic payment failure")

    monkeypatch.setattr(checkout_service, "record_payment", fail_payment)
    assert checkout(visit, amount=50000).status_code == 409
    assert snapshot(visit) == before


def test_completion_failure_rolls_back_payment_and_history(visit, monkeypatch):
    make_standard(visit)
    before = snapshot(visit)

    def fail_completion(*args, **kwargs):
        raise AppointmentOperationError("synthetic completion failure")

    monkeypatch.setattr(checkout_service, "update_operational_status_operation", fail_completion)
    assert checkout(visit, amount=50000).status_code == 409
    assert snapshot(visit) == before


def test_package_consumption_failure_rolls_back(visit, monkeypatch):
    before = snapshot(visit)

    def fail_consumption(*args, **kwargs):
        raise PackageOperationError("synthetic entitlement failure")

    monkeypatch.setattr(appointment_operations, "consume_visit_package_usages", fail_consumption)
    assert checkout(visit).status_code == 409
    assert snapshot(visit) == before


def test_snapshot_or_activity_failure_rolls_back(visit, monkeypatch):
    make_standard(visit)
    before = snapshot(visit)

    def fail_activity(*args, **kwargs):
        raise RuntimeError("synthetic activity failure")

    monkeypatch.setattr(checkout_service, "record_activity_event", fail_activity)
    with pytest.raises(RuntimeError, match="synthetic activity failure"):
        checkout(visit, amount=50000)
    assert snapshot(visit) == before


def test_commit_failure_rolls_back_in_same_api_session(visit):
    make_standard(visit)
    before = snapshot(visit)

    def failing_session():
        with visit["sessions"]() as db:
            def fail_commit():
                raise RuntimeError("synthetic commit failure")

            db.commit = fail_commit
            yield db

    visit["app"].dependency_overrides[get_db] = failing_session
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        checkout(visit, amount=50000)
    assert snapshot(visit) == before


def test_missing_appointment_rejected_without_payment(visit):
    response = visit["client"].post(
        "/payments/appointments/" + str(uuid4()) + "/checkout",
        json={"amount_minor": 0, "payment_method": "cash"},
        headers={"Idempotency-Key": "absent"},
    )
    assert response.status_code == 404


def test_member_without_clinic_operations_cannot_checkout(visit):
    visit["member"].role = "member"
    visit["member"].section_access = {
        "clinic_operations": False,
        "administration": False,
    }
    before = snapshot(visit)
    result = checkout(visit)
    assert result.status_code == 403
    assert snapshot(visit) == before


def test_discount_and_payment_complete_without_extra_charge(visit):
    make_standard(visit)
    response = checkout(visit, amount=40000, discount=10000, key="discount-checkout")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["subtotal_minor"] == 50000
    assert result["discount_minor"] == 10000
    assert result["balance_minor"] == 0
    assert snapshot(visit)["status"] == "completed"
    assert snapshot(visit)["payments"] == 1


def test_pulse_settlement_failure_rolls_back_even_after_mutation(visit, monkeypatch):
    from app.services.pulse_billing import PulseBillingError

    before = snapshot(visit)

    def fail_pulse(db, **kwargs):
        row = db.get(Appointment, visit["appointment"].id)
        row.billing_context = "pulse_prepaid"
        db.flush()
        raise PulseBillingError("synthetic pulse settlement failure")

    monkeypatch.setattr(checkout_service, "checkout_appointment_pulses", fail_pulse)
    response = checkout(visit, pulse_mode="overage", key="pulse-failed")
    assert response.status_code == 409
    assert snapshot(visit) == before
    with visit["sessions"]() as db:
        assert db.get(Appointment, visit["appointment"].id).billing_context == "package_prepaid"


def test_payment_snapshot_failure_rolls_back_payment_and_completion(visit, monkeypatch):
    from app.services import payments as payment_service

    make_standard(visit)
    before = snapshot(visit)

    def fail_snapshot(*args, **kwargs):
        raise RuntimeError("synthetic payment snapshot failure")

    monkeypatch.setattr(payment_service, "sync_appointment_payment_snapshot", fail_snapshot)
    with pytest.raises(RuntimeError, match="synthetic payment snapshot failure"):
        checkout(visit, amount=50000, key="snapshot-failure")
    assert snapshot(visit) == before


def test_patient_packages_api_returns_both_updated_balances_after_checkout(visit):
    """Exercise the actual GET endpoint used by the reception balance panel."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
    from app.api.routes import booking as booking_route

    added = add_additional(visit)
    assert added.status_code == 201, added.text
    assert checkout(visit, key="api-package-balance-read").status_code == 200

    api = FastAPI()
    api.include_router(booking_route.router, prefix="/booking")
    access = WorkspaceAccess(
        user=visit["user"],
        workspace=visit["workspace"],
        membership=visit["member"],
    )
    api.dependency_overrides[get_workspace_access] = lambda: access

    def test_db():
        with visit["sessions"]() as session:
            yield session

    api.dependency_overrides[get_db] = test_db
    with TestClient(api) as client:
        response = client.get(
            "/booking/patients/" + str(visit["patient"].id) + "/packages"
        )
    assert response.status_code == 200, response.text
    packages = {item["id"]: item for item in response.json()}
    assert len(packages) == 2
    for package_id in (visit["main_pkg"].id, visit["face_pkg"].id):
        row = packages[str(package_id)]
        assert row["sessions_remaining"] == 1
        assert row["sessions_consumed"] == 1
        assert row["sessions_reserved"] == 0
        assert row["effective_status"] == "active"
        assert row["laser_device_key"] == "candela"


def _seed_synthetic_reminder(visit):
    with visit["sessions"]() as db:
        rule = AutomationRule(
            workspace_id=visit["workspace"].id,
            key="reminder-" + uuid4().hex[:12],
            name="Synthetic booking reminder",
            trigger_kind="before_appointment",
            channel="whatsapp",
            template_name="synthetic_template",
        )
        db.add(rule)
        db.flush()
        job = AutomationJob(
            workspace_id=visit["workspace"].id,
            rule_id=rule.id,
            appointment_id=visit["appointment"].id,
            patient_id=visit["patient"].id,
            job_kind="appointment_rule",
            status="queued",
            scheduled_for=datetime.now(UTC) + timedelta(hours=1),
            next_attempt_at=datetime.now(UTC) + timedelta(minutes=10),
            dedupe_key="checkout-regression-" + uuid4().hex,
        )
        db.add(job)
        db.commit()
        return job.id


def test_checkout_cancels_pending_appointment_reminders(visit):
    job_id = _seed_synthetic_reminder(visit)
    response = checkout(visit, key="reminder-cancellation")
    assert response.status_code == 200, response.text
    with visit["sessions"]() as db:
        job = db.get(AutomationJob, job_id)
        assert job.status == "cancelled"
        assert job.completed_at is not None
        assert job.next_attempt_at is None
        assert job.result_json["reason"] == "appointment_completed"
    assert snapshot(visit)["status"] == "completed"


def test_reminder_cancellation_and_completion_rollback_together(visit, monkeypatch):
    job_id = _seed_synthetic_reminder(visit)
    before = snapshot(visit)

    def fail_consumption(*args, **kwargs):
        raise PackageOperationError("synthetic package failure after reminder cancellation")

    monkeypatch.setattr(appointment_operations, "consume_visit_package_usages", fail_consumption)
    response = checkout(visit, key="failed-reminder-completion")
    assert response.status_code == 409
    assert snapshot(visit) == before
    with visit["sessions"]() as db:
        job = db.get(AutomationJob, job_id)
        assert job.status == "queued"
        assert job.completed_at is None
        assert job.next_attempt_at is not None
