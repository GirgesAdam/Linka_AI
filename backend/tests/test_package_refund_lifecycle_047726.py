from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.services import appointment_operations as appointment_ops
from app.services import patient_packages as package_service
from app.services.patient_packages import PackageOperationError


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Patient.__table__.create(engine)
    Service.__table__.create(engine)
    PaymentTransaction.__table__.create(engine)
    PatientPackage.__table__.create(engine)
    PackageUsage.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE appointments (
                    id CHAR(32) PRIMARY KEY,
                    workspace_id CHAR(32),
                    patient_package_id CHAR(32),
                    billing_context VARCHAR(32),
                    package_external_id VARCHAR(128),
                    payment_status VARCHAR(16),
                    amount_paid_minor INTEGER,
                    payment_method VARCHAR(32),
                    status VARCHAR(20),
                    updated_at DATETIME
                )
                """
            )
        )
    return engine


def _seed_patient_service(db: Session, *, service_price_minor: int = 250_000):
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()
    db.add(
        Patient(
            id=patient_id,
            workspace_id=workspace_id,
            first_name="Mona",
            last_name="Ali",
            phone="01012345678",
            phone_normalized="01012345678",
            preferred_language="ar",
            source="other",
            status="active",
            marketing_consent=False,
        )
    )
    db.add(
        Service(
            id=service_id,
            workspace_id=workspace_id,
            name="Full Body Laser",
            slug=f"full-body-{service_id}",
            duration_minutes=60,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            price_minor=service_price_minor,
            currency="EGP",
            requires_medical_review=False,
            is_active=True,
        )
    )
    db.flush()
    return workspace_id, patient_id, service_id


def _appointment(*, workspace_id, patient_id, service_id):
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        patient_id=patient_id,
        service_id=service_id,
        patient_package_id=None,
        billing_context="standard",
        package_external_id=None,
        payment_status="unknown",
        amount_paid_minor=None,
        payment_method="unknown",
    )


def _allow_package_writes(monkeypatch):
    monkeypatch.setattr(package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        package_service, "refresh_appointment_payment_snapshots", lambda *args, **kwargs: None
    )


def test_package_refund_reprices_consumed_sessions_at_purchase_day_standalone_price(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(
            db, service_price_minor=250_000
        )
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 Full Body sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            payment_method="card",
            created_by_user_id=None,
            idempotency_key="full-body-package",
        )
        assert package.standalone_session_price_minor_at_purchase == 250_000

        consumed = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=consumed, package=package)
        package_service.consume_package_usage(db, appointment=consumed)

        future = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=future, package=package)

        (
            cancelled,
            collected,
            consumed_value,
            settlement_target,
            previously_refunded,
            collected_now,
            refunded_now,
            refunds,
        ) = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Client cancelled remaining package",
            created_by_user_id=uuid4(),
            idempotency_key="cancel-refund-1",
        )

        assert collected == 1_200_000
        assert consumed_value == 250_000
        assert settlement_target == 250_000
        assert previously_refunded == 0
        assert collected_now == 0
        assert refunded_now == 950_000
        assert sum(row.amount_minor for row in refunds) == 950_000
        assert cancelled.status == "cancelled"
        statuses = db.execute(
            select(PackageUsage.status, func.count())
            .where(PackageUsage.patient_package_id == package.id)
            .group_by(PackageUsage.status)
        ).all()
        assert dict(statuses) == {"consumed": 1, "released": 1}


def test_package_installments_refund_only_actual_collections_and_never_overpay(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(
            db, service_price_minor=250_000
        )
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 Full Body sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            amount_paid_minor=600_000,
            payment_method="card",
            created_by_user_id=None,
            idempotency_key="installment-package",
        )
        package_service.record_package_payment(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            amount_minor=600_000,
            payment_method="card",
            created_by_user_id=None,
            idempotency_key="installment-2",
        )
        with pytest.raises(PackageOperationError, match="remaining package balance of 0"):
            package_service.record_package_payment(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                amount_minor=1,
                payment_method="cash",
                created_by_user_id=None,
            )

        consumed = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=consumed, package=package)
        package_service.consume_package_usage(db, appointment=consumed)

        result = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Cancel remaining sessions",
            created_by_user_id=uuid4(),
            idempotency_key="installment-refund",
        )
        assert result[1] == 1_200_000
        assert result[2] == 250_000
        assert result[6] == 950_000
        refunds = db.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.patient_package_id == package.id,
                PaymentTransaction.transaction_type == "refund",
            )
        ).all()
        assert sum(row.amount_minor for row in refunds) == 950_000
        assert all(row.reference_transaction_id is not None for row in refunds)

        count_before = len(refunds)
        repeated = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Cancel remaining sessions",
            created_by_user_id=uuid4(),
            idempotency_key="installment-refund",
        )
        count_after = db.scalar(
            select(func.count()).select_from(PaymentTransaction).where(
                PaymentTransaction.patient_package_id == package.id,
                PaymentTransaction.transaction_type == "refund",
            )
        )
        assert count_after == count_before
        assert repeated[6] == 0


def test_partial_collection_refunds_only_collected_cash_after_consumed_value(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(
            db, service_price_minor=250_000
        )
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 Full Body sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            amount_paid_minor=600_000,
            payment_method="cash",
            created_by_user_id=None,
        )
        consumed = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=consumed, package=package)
        package_service.consume_package_usage(db, appointment=consumed)
        result = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Stop package",
            created_by_user_id=uuid4(),
        )
        assert result[1] == 600_000
        assert result[2] == 250_000
        assert result[6] == 350_000



def test_package_cancellation_collects_missing_consumed_session_value(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(
            db, service_price_minor=250_000
        )
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 Full Body sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            amount_paid_minor=100_000,
            payment_method="cash",
            created_by_user_id=None,
        )
        consumed = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=consumed, package=package)
        package_service.consume_package_usage(db, appointment=consumed)

        result = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Client cancelled",
            created_by_user_id=uuid4(),
            payment_method="cash",
            idempotency_key="collect-settlement",
        )

        assert result[2] == 250_000
        assert result[3] == 250_000
        assert result[5] == 150_000
        assert result[6] == 0
        payments = db.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.patient_package_id == package.id,
                PaymentTransaction.transaction_type == "payment",
            )
        ).all()
        assert sum(row.amount_minor for row in payments) == 250_000


def test_package_cancellation_accepts_admin_settlement_override(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(
            db, service_price_minor=250_000
        )
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 Full Body sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            amount_paid_minor=600_000,
            payment_method="cash",
            created_by_user_id=None,
        )
        consumed = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=consumed, package=package)
        package_service.consume_package_usage(db, appointment=consumed)

        result = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Approved custom settlement",
            created_by_user_id=uuid4(),
            settlement_target_minor=300_000,
        )

        assert result[2] == 250_000
        assert result[3] == 300_000
        assert result[5] == 0
        assert result[6] == 300_000

def test_migrated_package_uses_service_price_for_settlement_and_persists_snapshot(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(
            db, service_price_minor=250_000
        )
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="Migrated package",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            amount_paid_minor=1_200_000,
            payment_method="card",
            created_by_user_id=None,
        )
        package.source = "integration"
        package.opening_sessions_remaining = 4
        package.sessions_total_known = True
        package.standalone_session_price_minor_at_purchase = None
        db.flush()

        read = package_service.package_read(db, package, include_financials=True)
        assert read.cancellation_consumed_sessions == 2
        assert read.cancellation_default_charge_minor == 500_000

        result = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Historical package settlement",
            created_by_user_id=uuid4(),
        )

        assert package.standalone_session_price_minor_at_purchase == 250_000
        assert result[2] == 500_000
        assert result[3] == 500_000
        assert result[6] == 700_000


def test_legacy_consumed_package_requires_admin_price_confirmation_before_refund(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(db)
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="Legacy package",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            payment_method="card",
            created_by_user_id=None,
        )
        package.standalone_session_price_minor_at_purchase = None
        consumed = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=consumed, package=package)
        package_service.consume_package_usage(db, appointment=consumed)
        with pytest.raises(PackageOperationError, match="Standalone session price"):
            package_service.cancel_patient_package_with_refund(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                reason="Legacy cancellation",
                created_by_user_id=uuid4(),
            )

        result = package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Legacy cancellation",
            created_by_user_id=uuid4(),
            standalone_session_price_minor_at_purchase=240_000,
        )
        assert package.standalone_session_price_minor_at_purchase == 240_000
        assert result[6] == 960_000


def test_no_show_releases_package_reservation_like_cancellation(monkeypatch) -> None:
    appointment = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        status="confirmed",
        start_at=datetime.now(UTC) - timedelta(hours=2),
        completed_at=None,
        no_show_at=None,
    )
    released: list[str] = []
    monkeypatch.setattr(appointment_ops, "_locked_appointment", lambda *args, **kwargs: appointment)
    monkeypatch.setattr(appointment_ops, "add_appointment_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        appointment_ops, "cancel_pending_appointment_jobs", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(appointment_ops, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        appointment_ops,
        "release_visit_package_usages",
        lambda *args, **kwargs: released.append(kwargs["reason"]),
    )
    monkeypatch.setattr(
        appointment_ops,
        "release_appointment_pulse_usage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        appointment_ops,
        "consume_visit_package_usages",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no-show must not consume")),
    )
    db = SimpleNamespace(flush=lambda: None)
    result = appointment_ops.update_operational_status_operation(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        target_status="no_show",
        changed_by_user_id=uuid4(),
        now=datetime.now(UTC),
    )
    assert result.status == "no_show"
    assert released == ["patient_no_show"]


def test_package_cancellation_detaches_future_reserved_appointments_without_cancelling_them(monkeypatch) -> None:
    _allow_package_writes(monkeypatch)
    engine = _engine()
    refreshed: list[set] = []

    def _refresh(db, *, workspace_id, appointment_ids):
        refreshed.append(set(appointment_ids))
        for appointment_id in appointment_ids:
            db.execute(
                text(
                    "UPDATE appointments SET payment_status='unpaid', amount_paid_minor=0, "
                    "payment_method='unknown' WHERE workspace_id=:workspace_id AND id=:appointment_id"
                ),
                {
                    "workspace_id": workspace_id.hex,
                    "appointment_id": appointment_id.hex,
                },
            )

    monkeypatch.setattr(package_service, "refresh_appointment_payment_snapshots", _refresh)

    with Session(engine) as db:
        workspace_id, patient_id, service_id = _seed_patient_service(
            db, service_price_minor=250_000
        )
        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 Full Body sessions",
            sessions_purchased=6,
            sale_price_minor=1_200_000,
            payment_method="card",
            created_by_user_id=None,
        )
        future = _appointment(
            workspace_id=workspace_id, patient_id=patient_id, service_id=service_id
        )
        package_service.reserve_package_usage(db, appointment=future, package=package)
        db.execute(
            text(
                """
                INSERT INTO appointments
                    (id, workspace_id, patient_package_id, billing_context, package_external_id,
                     payment_status, amount_paid_minor, payment_method, status)
                VALUES
                    (:id, :workspace_id, :package_id, 'package_prepaid', :package_external_id,
                     'paid', NULL, 'unknown', 'confirmed')
                """
            ),
            {
                "id": future.id.hex,
                "workspace_id": workspace_id.hex,
                "package_id": package.id.hex,
                "package_external_id": package.external_id or str(package.id),
            },
        )

        package_service.cancel_patient_package_with_refund(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            reason="Cancel package but keep future appointment",
            created_by_user_id=uuid4(),
        )

        row = db.execute(
            text(
                "SELECT patient_package_id, billing_context, package_external_id, payment_status, "
                "amount_paid_minor, payment_method, status FROM appointments WHERE id=:id"
            ),
            {"id": future.id.hex},
        ).one()
        assert row.patient_package_id is None
        assert row.billing_context == "standard"
        assert row.package_external_id is None
        assert row.payment_status == "unpaid"
        assert row.amount_paid_minor == 0
        assert row.payment_method == "unknown"
        assert row.status == "confirmed"
        assert refreshed == [{future.id}]

        usage = db.scalar(
            select(PackageUsage).where(PackageUsage.appointment_id == future.id)
        )
        assert usage is not None and usage.status == "released"


def test_completed_standard_appointment_ignores_released_historical_package_usage(monkeypatch) -> None:
    appointment = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        patient_package_id=None,
        billing_context="standard",
        status="confirmed",
        start_at=datetime.now(UTC) - timedelta(hours=2),
        completed_at=None,
        no_show_at=None,
    )
    monkeypatch.setattr(appointment_ops, "_locked_appointment", lambda *args, **kwargs: appointment)
    monkeypatch.setattr(appointment_ops, "add_appointment_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        appointment_ops, "cancel_pending_appointment_jobs", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(appointment_ops, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        appointment_ops,
        "consume_visit_package_usages",
        lambda *args, **kwargs: [],
    )
    db = SimpleNamespace(flush=lambda: None)
    result = appointment_ops.update_operational_status_operation(
        db,
        workspace_id=appointment.workspace_id,
        appointment_id=appointment.id,
        target_status="completed",
        changed_by_user_id=uuid4(),
        now=datetime.now(UTC),
    )
    assert result.status == "completed"
    assert result.completed_at is not None
