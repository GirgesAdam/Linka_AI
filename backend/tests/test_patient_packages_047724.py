from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.integrations.clinic.tabular_import import TabularWorkbook, build_import_preview
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.schemas.booking import AppointmentCreate
from app.schemas.clinic_import import (
    AppointmentSheetMapping,
    BranchHoursSheetMapping,
    BranchSheetMapping,
    ClinicImportMapping,
    PackageSheetMapping,
    PackageUsageSheetMapping,
    ServiceSheetMapping,
)
from app.services import patient_packages as package_service
from app.services.patient_packages import PackageOperationError


def _package_engine(*, with_commercial_tables: bool = False):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    if with_commercial_tables:
        Patient.__table__.create(engine)
        Service.__table__.create(engine)
        PaymentTransaction.__table__.create(engine)
    PatientPackage.__table__.create(engine)
    PackageUsage.__table__.create(engine)
    return engine


def _package(*, workspace_id, patient_id, service_id, sessions=6) -> PatientPackage:
    return PatientPackage(
        workspace_id=workspace_id,
        patient_id=patient_id,
        service_id=service_id,
        purchase_transaction_id=None,
        created_by_user_id=None,
        external_id="PKG-00005",
        name="6 sessions - Laser Bikini",
        sessions_purchased=sessions,
        sale_price_minor=480_000,
        standalone_session_price_minor_at_purchase=100_000,
        currency="EGP",
        purchased_at=datetime.now(UTC) - timedelta(days=30),
        expires_at=date.today() + timedelta(days=300),
        status="active",
        source="integration",
    )


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


def test_six_session_package_blocks_seventh_future_reservation_and_cancel_returns_credit(monkeypatch) -> None:
    monkeypatch.setattr(package_service, "record_activity_event", lambda *args, **kwargs: None)
    engine = _package_engine()
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()

    with Session(engine) as db:
        package = _package(
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
        )
        db.add(package)
        db.flush()

        appointments = []
        for day in range(1, 7):
            package_service.validate_package_for_booking(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                patient_id=patient_id,
                service_id=service_id,
                appointment_start_at=datetime.now(UTC) + timedelta(days=day),
            )
            appointment = _appointment(
                workspace_id=workspace_id,
                patient_id=patient_id,
                service_id=service_id,
            )
            package_service.reserve_package_usage(db, appointment=appointment, package=package)
            appointments.append(appointment)

        summary = package_service.package_read(db, package)
        assert summary.sessions_reserved == 6
        assert summary.sessions_consumed == 0
        assert summary.sessions_remaining == 0
        assert summary.effective_status == "exhausted"

        with pytest.raises(PackageOperationError, match="0 session"):
            package_service.validate_package_for_booking(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                patient_id=patient_id,
                service_id=service_id,
                appointment_start_at=datetime.now(UTC) + timedelta(days=7),
            )

        # A cancellation before treatment releases the reservation.
        package_service.release_package_usage(
            db,
            appointment=appointments[1],
            reason="appointment_cancelled",
        )
        summary = package_service.package_read(db, package)
        assert summary.sessions_reserved == 5
        assert summary.sessions_remaining == 1

        # The newly available credit can now be reserved by a replacement visit.
        package_service.validate_package_for_booking(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            patient_id=patient_id,
            service_id=service_id,
            appointment_start_at=datetime.now(UTC) + timedelta(days=7),
        )




def test_migrated_opening_balance_is_used_for_booking_and_new_usage(monkeypatch) -> None:
    monkeypatch.setattr(package_service, "record_activity_event", lambda *args, **kwargs: None)
    engine = _package_engine()
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as db:
        package = _package(
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            sessions=10,
        )
        package.opening_sessions_remaining = 4
        package.sessions_total_known = True
        db.add(package)
        db.flush()

        before = package_service.package_read(db, package)
        assert before.sessions_purchased == 10
        assert before.sessions_remaining == 4

        package_service.validate_package_for_booking(
            db,
            workspace_id=workspace_id,
            package_id=package.id,
            patient_id=patient_id,
            service_id=service_id,
            appointment_start_at=datetime.now(UTC) + timedelta(days=2),
        )
        appointment = _appointment(
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
        )
        package_service.reserve_package_usage(db, appointment=appointment, package=package)

        after = package_service.package_read(db, package)
        assert after.sessions_reserved == 1
        assert after.sessions_remaining == 3


def test_completion_is_idempotent_and_reschedule_transfers_same_package_reservation(monkeypatch) -> None:
    monkeypatch.setattr(package_service, "record_activity_event", lambda *args, **kwargs: None)
    engine = _package_engine()
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()

    with Session(engine) as db:
        package = _package(workspace_id=workspace_id, patient_id=patient_id, service_id=service_id)
        db.add(package)
        db.flush()

        first = _appointment(workspace_id=workspace_id, patient_id=patient_id, service_id=service_id)
        package_service.reserve_package_usage(db, appointment=first, package=package)
        package_service.consume_package_usage(db, appointment=first, used_at=datetime.now(UTC))
        package_service.consume_package_usage(db, appointment=first, used_at=datetime.now(UTC))
        usage = db.scalar(select(PackageUsage).where(PackageUsage.appointment_id == first.id))
        assert usage is not None and usage.status == "consumed"
        assert package_service.package_read(db, package).sessions_consumed == 1

        current = _appointment(workspace_id=workspace_id, patient_id=patient_id, service_id=service_id)
        replacement = _appointment(workspace_id=workspace_id, patient_id=patient_id, service_id=service_id)
        package_service.reserve_package_usage(db, appointment=current, package=package)
        before = package_service.package_read(db, package)
        package_service.transfer_package_usage(
            db,
            from_appointment=current,
            to_appointment=replacement,
        )
        after = package_service.package_read(db, package)
        assert before.sessions_reserved == after.sessions_reserved == 1
        moved = db.scalar(select(PackageUsage).where(PackageUsage.appointment_id == replacement.id))
        assert moved is not None and moved.patient_package_id == package.id
        assert replacement.billing_context == "package_prepaid"
        assert replacement.patient_package_id == package.id


def test_package_wrong_patient_service_or_expired_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(package_service, "record_activity_event", lambda *args, **kwargs: None)
    engine = _package_engine()
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as db:
        package = _package(workspace_id=workspace_id, patient_id=patient_id, service_id=service_id)
        db.add(package)
        db.flush()
        future = datetime.now(UTC) + timedelta(days=2)
        with pytest.raises(PackageOperationError, match="another patient"):
            package_service.validate_package_for_booking(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                patient_id=uuid4(),
                service_id=service_id,
                appointment_start_at=future,
            )
        with pytest.raises(PackageOperationError, match="different service"):
            package_service.validate_package_for_booking(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                patient_id=patient_id,
                service_id=uuid4(),
                appointment_start_at=future,
            )
        package.expires_at = date.today()
        with pytest.raises(PackageOperationError, match="not active"):
            package_service.validate_package_for_booking(
                db,
                workspace_id=workspace_id,
                package_id=package.id,
                patient_id=patient_id,
                service_id=service_id,
                appointment_start_at=datetime.now(UTC) + timedelta(days=1),
            )


def test_new_package_sale_records_one_patient_level_payment_and_is_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(package_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(package_service, "require_tia_workspace_domain_write", lambda *args, **kwargs: None)
    engine = _package_engine(with_commercial_tables=True)
    workspace_id, patient_id, service_id = uuid4(), uuid4(), uuid4()

    with Session(engine) as db:
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
                name="Laser Bikini",
                slug="laser-bikini",
                duration_minutes=30,
                buffer_before_minutes=0,
                buffer_after_minutes=0,
                price_minor=100_000,
                currency="EGP",
                requires_medical_review=False,
                is_active=True,
            )
        )
        db.flush()

        package = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 sessions - Laser Bikini",
            sessions_purchased=6,
            sale_price_minor=480_000,
            payment_method="card",
            created_by_user_id=None,
            expires_at=date.today() + timedelta(days=365),
            external_reference="POS-PKG-00005",
            idempotency_key="pkg-sale-00005",
        )
        same = package_service.create_patient_package(
            db,
            workspace_id=workspace_id,
            patient_id=patient_id,
            service_id=service_id,
            name="6 sessions - Laser Bikini",
            sessions_purchased=6,
            sale_price_minor=480_000,
            payment_method="card",
            created_by_user_id=None,
            expires_at=date.today() + timedelta(days=365),
            external_reference="POS-PKG-00005",
            idempotency_key="pkg-sale-00005",
        )
        assert same.id == package.id
        payments = db.scalars(select(PaymentTransaction)).all()
        assert len(payments) == 1
        assert payments[0].amount_minor == 480_000
        assert payments[0].appointment_id is None
        assert payments[0].origin_appointment_id is None
        assert package.purchase_transaction_id == payments[0].id
        assert db.scalar(select(func.count()).select_from(PackageUsage)) == 0


def _realistic_package_workbook(*, appointment_count: int = 1) -> tuple[TabularWorkbook, ClinicImportMapping]:
    appointments = []
    for index in range(appointment_count):
        appointments.append(
            {
                "booking_ref": f"A2026-{index + 1:06d}",
                "patient_ref": "P00103",
                "client_name": "Mona Ali",
                "mobile": "+20 101 234 5678",
                "service_code": "SVC-LAS-BIK",
                "date": f"2026-08-{index + 1:02d}",
                "time": "10:00",
                "status": "Done",
                "payment_context": "package_prepaid",
                "package_id": "PKG-00005",
            }
        )
    workbook = TabularWorkbook(
        sheets={
            "services": [
                {
                    "service_code": "SVC-LAS-BIK",
                    "service_name": "Laser Bikini",
                    "duration": "30",
                    "price": "1000",
                }
            ],
            "branches": [{"branch_code": "NASR", "branch_name": "Nasr City"}],
            "branch_hours": [
                {"branch": "NASR", "day": day, "start": "09:00", "end": "21:00"}
                for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            ],
            "appointments": appointments,
            "package_sales": [
                {
                    "package_id": "PKG-00005",
                    "patient_ref": "P00103",
                    "service_code": "SVC-LAS-BIK",
                    "package_name": "6 sessions - Laser Bikini",
                    "sessions_purchased": "6",
                    "sale_price_egp": "4800",
                    "sold_at": "2026-01-24T09:15+02:00",
                    "expires_at": "2027-01-24",
                    "status": "active",
                }
            ],
            "package_usage": [
                {
                    "usage_id": "USE-000001",
                    "package_id": "PKG-00005",
                    "appointment_ref": "A2026-000001",
                    "sessions_used": "1",
                    "used_at": "2026-08-01T10:30+03:00",
                }
            ] if appointment_count == 1 else [],
        },
        document_names=("realistic-package-export",),
    )
    mapping = ClinicImportMapping(
        services=ServiceSheetMapping(
            sheet="services",
            external_id="service_code",
            name="service_name",
            duration_minutes="duration",
            price="price",
        ),
        branches=BranchSheetMapping(
            sheet="branches",
            external_id="branch_code",
            name="branch_name",
            default_timezone="Africa/Cairo",
        ),
        branch_hours=BranchHoursSheetMapping(
            sheet="branch_hours",
            branch_external_id="branch",
            weekday="day",
            start_time="start",
            end_time="end",
        ),
        appointments=AppointmentSheetMapping(
            sheet="appointments",
            external_id="booking_ref",
            patient_external_id="patient_ref",
            patient_name="client_name",
            patient_phone="mobile",
            service_external_id="service_code",
            default_branch_external_id="NASR",
            appointment_date="date",
            appointment_time="time",
            status="status",
            payment_context="payment_context",
            package_external_id="package_id",
            default_timezone="Africa/Cairo",
        ),
        packages=PackageSheetMapping(
            sheet="package_sales",
            external_id="package_id",
            patient_external_id="patient_ref",
            service_external_id="service_code",
            name="package_name",
            sessions_purchased="sessions_purchased",
            sale_price="sale_price_egp",
            sold_at="sold_at",
            expires_at="expires_at",
            status="status",
            default_timezone="Africa/Cairo",
        ),
        package_usages=(
            PackageUsageSheetMapping(
                sheet="package_usage",
                external_id="usage_id",
                package_external_id="package_id",
                appointment_external_id="appointment_ref",
                sessions_used="sessions_used",
                used_at="used_at",
                default_timezone="Africa/Cairo",
            )
            if appointment_count == 1
            else None
        ),
    )
    return workbook, mapping


def test_realistic_package_sale_and_usage_are_normalized_without_creating_session_revenue() -> None:
    workbook, mapping = _realistic_package_workbook()
    preview = build_import_preview(workbook, mapping)
    assert preview.can_apply is True
    assert len(preview.packages) == 1
    assert len(preview.package_usages) == 1
    package = preview.packages[0]
    appointment = preview.appointments[0]
    assert package.external_id == "PKG-00005"
    assert package.sessions_purchased == 6
    assert package.sale_price_minor == 480_000
    assert appointment.billing_context == "package_prepaid"
    assert appointment.amount_paid_minor is None
    assert appointment.payment_status == "paid"
    assert not [issue for issue in preview.issues if issue.severity == "error"]


def test_import_detects_seventh_session_even_without_separate_usage_file() -> None:
    workbook, mapping = _realistic_package_workbook(appointment_count=7)
    preview = build_import_preview(workbook, mapping)
    errors = [issue for issue in preview.issues if issue.severity == "error"]
    assert any(issue.code == "package_usage_exceeds_entitlement" for issue in errors)
    assert preview.can_apply is False


def test_booking_schema_preserves_selected_package_id() -> None:
    package_id = uuid4()
    payload = AppointmentCreate(
        patient_id=uuid4(),
        branch_id=uuid4(),
        doctor_id=uuid4(),
        service_id=uuid4(),
        patient_package_id=package_id,
        start_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert payload.patient_package_id == package_id
