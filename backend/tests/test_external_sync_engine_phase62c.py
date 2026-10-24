from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.integrations.clinic.structural_transform import apply_structural_transforms
from app.integrations.clinic.sync_contract import (
    ClinicSyncDomain,
    ClinicSyncPage,
    ExternalPatientSyncRecord,
    ExternalPaymentAllocationSyncRecord,
    ExternalPaymentSyncRecord,
)
from app.models.appointment import Appointment
from app.models.clinic_integration import ClinicIntegrationEntityLink
from app.models.clinic_integration_sync import (
    ClinicIntegrationSyncCheckpoint,
    ClinicIntegrationSyncFailure,
)
from app.models.patient import Patient
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.schemas.clinic_import import (
    StructuralFieldMapping,
    StructuralJoinKey,
    StructuralJoinMapping,
    StructuralTransformMapping,
)
from app.services import clinic_integration_sync as sync_service
from app.services.clinic_integration_sync import (
    ClinicIntegrationSyncError,
    apply_external_sync_page,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _create_sqlite_schema(engine) -> None:
    ddl = [
        """
        CREATE TABLE clinic_integrations (
            workspace_id CHAR(32) PRIMARY KEY,
            mode VARCHAR(32), adapter_key VARCHAR(80), status VARCHAR(24),
            external_clinic_id VARCHAR(255), secret_ref VARCHAR(512), config TEXT,
            authority_policy TEXT DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE patients (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            first_name VARCHAR(120), last_name VARCHAR(120), phone VARCHAR(40),
            phone_normalized VARCHAR(40), customer_id TEXT, gender VARCHAR(32),
            birth_date DATE, source_created_at DATETIME, preferred_language VARCHAR(10), preferred_branch_id CHAR(32),
            source VARCHAR(32), source_detail VARCHAR(200), status VARCHAR(20),
            marketing_consent BOOLEAN DEFAULT 0, marketing_consent_at DATETIME, whatsapp_opt_in BOOLEAN DEFAULT 0,
            whatsapp_opt_in_at DATETIME, whatsapp_opt_in_source VARCHAR(32),
            last_contact_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE UNIQUE INDEX uq_test_patients_phone ON patients(workspace_id, phone_normalized) WHERE phone_normalized IS NOT NULL",
        """
        CREATE TABLE clinic_integration_entity_links (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), entity_type VARCHAR(32),
            canonical_id VARCHAR(255), external_id VARCHAR(512), metadata TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE UNIQUE INDEX uq_test_link_external ON clinic_integration_entity_links(workspace_id, entity_type, external_id)",
        "CREATE UNIQUE INDEX uq_test_link_canonical ON clinic_integration_entity_links(workspace_id, entity_type, canonical_id)",
        """
        CREATE TABLE clinic_integration_sync_runs (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), domain VARCHAR(24),
            status VARCHAR(20), cursor_before VARCHAR(512), cursor_after VARCHAR(512),
            source_revision VARCHAR(255), processed_count INTEGER DEFAULT 0,
            created_count INTEGER DEFAULT 0, updated_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME
        )
        """,
        """
        CREATE TABLE clinic_integration_sync_checkpoints (
            workspace_id CHAR(32), domain VARCHAR(24), cursor VARCHAR(512),
            source_revision VARCHAR(255), last_success_at DATETIME, last_run_id CHAR(32),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(workspace_id, domain)
        )
        """,
        """
        CREATE TABLE clinic_integration_sync_failures (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), run_id CHAR(32),
            domain VARCHAR(24), external_id_digest VARCHAR(64), error_code VARCHAR(80),
            message VARCHAR(300), retryable BOOLEAN DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE appointments (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), patient_id CHAR(32),
            branch_id CHAR(32), doctor_id CHAR(32), doctor_assignment_known BOOLEAN DEFAULT 1, service_id CHAR(32), patient_package_id CHAR(32), visit_group_id CHAR(32), lead_id CHAR(32),
            created_by_user_id CHAR(32), rescheduled_from_appointment_id CHAR(32),
            status VARCHAR(20), source VARCHAR(20), start_at DATETIME, end_at DATETIME,
            busy_start_at DATETIME, busy_end_at DATETIME, duration_minutes INTEGER,
            price_minor INTEGER, discount_minor INTEGER DEFAULT 0, is_quick_booking BOOLEAN DEFAULT 0, laser_pulses_used INTEGER, currency VARCHAR(3), laser_device_key VARCHAR(40), laser_device_name VARCHAR(120), payment_status VARCHAR(16),
            amount_paid_minor INTEGER, payment_method VARCHAR(20), billing_context VARCHAR(24) DEFAULT 'standard', package_external_id VARCHAR(128), customer_note TEXT,
            cancellation_reason TEXT, idempotency_key VARCHAR(128), confirmed_at DATETIME,
            cancelled_at DATETIME, completed_at DATETIME, no_show_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE appointment_product_lines (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            appointment_id CHAR(32) NOT NULL, product_id CHAR(32) NOT NULL,
            product_name VARCHAR(180) NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
            unit_price_minor INTEGER NOT NULL, currency VARCHAR(3) NOT NULL DEFAULT 'EGP',
            created_by_user_id CHAR(32), created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE appointment_additional_services (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            appointment_id CHAR(32) NOT NULL, service_id CHAR(32) NOT NULL,
            service_name VARCHAR(200) NOT NULL, unit_price_minor INTEGER NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'EGP',
            laser_device_key VARCHAR(40), laser_device_name VARCHAR(120),
            patient_package_id CHAR(32), billing_context VARCHAR(24) DEFAULT 'standard',
            laser_pulses_used INTEGER, pulse_resolution VARCHAR(16),
            pulse_resolution_pulse_pack_id CHAR(32),
            pulse_overage_unit_price_minor INTEGER,
            pulse_overage_charge_minor INTEGER NOT NULL DEFAULT 0,
            created_by_user_id CHAR(32), created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE patient_packages (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            patient_id CHAR(32) NOT NULL, service_id CHAR(32) NOT NULL,
            purchase_transaction_id CHAR(32), package_offer_id CHAR(32),
            origin_appointment_id CHAR(32), created_by_user_id CHAR(32),
            external_id VARCHAR(128), name VARCHAR(200) NOT NULL,
            sessions_purchased INTEGER NOT NULL, opening_sessions_remaining INTEGER,
            sessions_total_known BOOLEAN NOT NULL DEFAULT 1,
            sale_price_minor INTEGER NOT NULL DEFAULT 0,
            standalone_session_price_minor_at_purchase INTEGER,
            laser_device_key VARCHAR(40), laser_device_name VARCHAR(120),
            currency VARCHAR(3) NOT NULL DEFAULT 'EGP',
            purchased_at DATETIME NOT NULL, expires_at DATE,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            source VARCHAR(16) NOT NULL DEFAULT 'staff',
            idempotency_key VARCHAR(128),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE payment_transactions (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), appointment_id CHAR(32),
            origin_appointment_id CHAR(32), patient_id CHAR(32), created_by_user_id CHAR(32),
            reference_transaction_id CHAR(32), patient_package_id CHAR(32), patient_pulse_pack_id CHAR(32), transaction_type VARCHAR(16), amount_minor INTEGER,
            currency VARCHAR(3), payment_method VARCHAR(24), source VARCHAR(24),
            external_reference VARCHAR(128), reason TEXT, idempotency_key VARCHAR(128),
            created_at DATETIME
        )
        """,
        "CREATE UNIQUE INDEX uq_test_payment_idempotency ON payment_transactions(workspace_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
        """
        CREATE TABLE patient_pulse_packs (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), patient_id CHAR(32),
            origin_appointment_id CHAR(32), sale_price_minor INTEGER DEFAULT 0,
            status VARCHAR(16) DEFAULT 'active'
        )
        """,
        """
        CREATE TABLE appointment_pulse_settlements (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), appointment_id CHAR(32),
            overage_charge_minor INTEGER DEFAULT 0, resolution VARCHAR(16) DEFAULT 'pending'
        )
        """,
        """
        CREATE TABLE payment_allocations (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), transaction_id CHAR(32),
            appointment_id CHAR(32), amount_minor INTEGER, created_at DATETIME
        )
        """,
        "CREATE UNIQUE INDEX uq_test_payment_allocation ON payment_allocations(workspace_id, transaction_id, appointment_id)",
    ]
    with engine.begin() as connection:
        for statement in ddl:
            connection.exec_driver_sql(statement)


@pytest.fixture()
def sync_db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _create_sqlite_schema(engine)
    workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO clinic_integrations "
                "(workspace_id, mode, adapter_key, status, config) "
                "VALUES (:workspace_id, 'external_api', 'prototype_external', 'active', '{}')"
            ),
            {"workspace_id": workspace_id.hex},
        )
    monkeypatch.setattr(sync_service, "record_activity_event", lambda *args, **kwargs: None)
    with Session(engine) as db:
        yield db, SimpleNamespace(id=workspace_id, timezone="Africa/Cairo")


def _add_link(
    db: Session,
    workspace_id: UUID,
    entity_type: str,
    canonical_id: UUID,
    external_id: str,
    *,
    metadata: dict | None = None,
) -> None:
    db.add(
        ClinicIntegrationEntityLink(
            workspace_id=workspace_id,
            entity_type=entity_type,
            canonical_id=str(canonical_id),
            external_id=external_id,
            metadata_json=metadata or {},
        )
    )
    db.flush()


def _insert_patient(
    db: Session,
    workspace_id: UUID,
    *,
    first_name: str = "Existing",
    phone: str | None = None,
    phone_normalized: str | None = None,
) -> Patient:
    patient = Patient(
        workspace_id=workspace_id,
        first_name=first_name,
        last_name=None,
        phone=phone,
        phone_normalized=phone_normalized,
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add(patient)
    db.flush()
    return patient


def _insert_appointment(
    db: Session,
    workspace_id: UUID,
    patient_id: UUID,
    *,
    external_id: str,
    price_minor: int = 100_000,
) -> Appointment:
    placeholder = uuid4()
    appointment = Appointment(
        workspace_id=workspace_id,
        patient_id=patient_id,
        branch_id=placeholder,
        doctor_id=placeholder,
        service_id=placeholder,
        status="confirmed",
        source="other",
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
        busy_start_at=NOW,
        busy_end_at=NOW + timedelta(hours=1),
        duration_minutes=60,
        price_minor=price_minor,
        currency="EGP",
        payment_status="unpaid",
        amount_paid_minor=0,
        payment_method="unknown",
    )
    db.add(appointment)
    db.flush()
    _add_link(db, workspace_id, "appointment", appointment.id, external_id)
    return appointment


def _patient_record(
    external_id: str,
    *,
    first_name: str = "Sara",
    phone: str | None = "+201001234567",
    status: str = "active",
    source_updated_at: datetime | None = NOW,
) -> ExternalPatientSyncRecord:
    return ExternalPatientSyncRecord(
        external_id=external_id,
        first_name=first_name,
        phone=phone,
        status=status,
        source="other",
        source_updated_at=source_updated_at,
    )


def test_patient_identity_resolution_prefers_external_link_then_verified_phone(sync_db) -> None:
    db, workspace = sync_db
    existing = _insert_patient(
        db,
        workspace.id,
        first_name="Old Name",
        phone="01001234567",
        phone_normalized="+201001234567",
    )

    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(_patient_record("CLIENT-1", first_name="Sara"),),
            next_cursor="patients:1",
        ),
        now=NOW,
    )
    db.commit()

    assert result.status == "succeeded"
    assert result.updated_count == 1
    assert db.scalar(select(Patient).where(Patient.workspace_id == workspace.id)).id == existing.id
    link = db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace.id,
            ClinicIntegrationEntityLink.entity_type == "patient",
            ClinicIntegrationEntityLink.external_id == "CLIENT-1",
        )
    )
    assert link is not None
    assert link.canonical_id == str(existing.id)

    replay = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            cursor="patients:1",
            next_cursor="patients:2",
            records=(_patient_record("CLIENT-1", first_name="Sara"),),
        ),
        now=NOW + timedelta(minutes=1),
    )
    db.commit()
    assert replay.skipped_count == 1
    assert db.query(Patient).count() == 1


def test_name_only_does_not_merge_without_phone_or_external_link(sync_db) -> None:
    db, workspace = sync_db
    existing = _insert_patient(db, workspace.id, first_name="Same Name")

    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(
                ExternalPatientSyncRecord(
                    external_id="NAME-ONLY-NEW",
                    first_name="Same Name",
                    phone=None,
                    source_updated_at=NOW,
                ),
            ),
            next_cursor="name-only:1",
        ),
        now=NOW,
    )
    db.commit()

    assert result.created_count == 1
    assert db.query(Patient).count() == 2
    link = db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace.id,
            ClinicIntegrationEntityLink.entity_type == "patient",
            ClinicIntegrationEntityLink.external_id == "NAME-ONLY-NEW",
        )
    )
    assert link is not None
    assert link.canonical_id != str(existing.id)


def test_patient_name_alone_never_merges_identity(sync_db) -> None:
    db, workspace = sync_db
    _insert_patient(db, workspace.id, first_name="Sara")

    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(_patient_record("CLIENT-2", phone=None, first_name="Sara"),),
            next_cursor="p:1",
        ),
        now=NOW,
    )
    db.commit()

    assert result.created_count == 1
    assert db.query(Patient).count() == 2


def test_partial_patient_page_isolates_failure_and_does_not_advance_cursor(sync_db) -> None:
    db, workspace = sync_db
    page = ClinicSyncPage(
        domain=ClinicSyncDomain.PATIENTS,
        records=(
            _patient_record("GOOD", phone="01011111111"),
            _patient_record("BAD", phone="01022222222", status="mystery"),
        ),
        next_cursor="p:next",
    )
    result = apply_external_sync_page(db=db, workspace=workspace, page=page, now=NOW)
    db.commit()

    assert result.status == "partial"
    assert result.created_count == 1
    assert result.failed_count == 1
    assert result.cursor_after is None
    assert db.get(ClinicIntegrationSyncCheckpoint, (workspace.id, "patients")) is None
    failure = db.scalar(select(ClinicIntegrationSyncFailure))
    assert failure is not None
    assert failure.error_code == "invalid_patient_status"
    assert failure.external_id_digest != "BAD"
    assert len(failure.external_id_digest) == 64

    retry = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(
                _patient_record("GOOD", phone="01011111111"),
                _patient_record("BAD", phone="01022222222", status="active"),
            ),
            next_cursor="p:next",
        ),
        now=NOW + timedelta(minutes=1),
    )
    db.commit()
    assert retry.status == "succeeded"
    assert retry.skipped_count == 1
    assert retry.created_count == 1
    checkpoint = db.get(ClinicIntegrationSyncCheckpoint, (workspace.id, "patients"))
    assert checkpoint is not None
    assert checkpoint.cursor == "p:next"


def test_same_source_version_with_changed_patient_fact_fails_closed(sync_db) -> None:
    db, workspace = sync_db
    first = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(_patient_record("CLIENT-3", first_name="Sara"),),
            next_cursor="p1",
        ),
        now=NOW,
    )
    db.commit()
    assert first.status == "succeeded"

    conflicting = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            cursor="p1",
            records=(_patient_record("CLIENT-3", first_name="Different"),),
            next_cursor="p2",
        ),
        now=NOW + timedelta(minutes=1),
    )
    db.commit()
    assert conflicting.status == "failed"
    assert conflicting.failed_count == 1
    assert conflicting.cursor_after == "p1"
    checkpoint = db.get(ClinicIntegrationSyncCheckpoint, (workspace.id, "patients"))
    assert checkpoint.cursor == "p1"


def test_multi_appointment_and_unallocated_payments_are_idempotent(sync_db) -> None:
    db, workspace = sync_db
    patient = _insert_patient(db, workspace.id, first_name="Mona")
    _add_link(db, workspace.id, "patient", patient.id, "C1")
    visit_1 = _insert_appointment(db, workspace.id, patient.id, external_id="V1")
    visit_2 = _insert_appointment(db, workspace.id, patient.id, external_id="V2")

    allocated = ExternalPaymentSyncRecord(
        external_id="R1",
        patient_external_id="C1",
        transaction_type="payment",
        amount_minor=150_000,
        currency="EGP",
        payment_method="card",
        created_at=NOW,
        allocations=(
            ExternalPaymentAllocationSyncRecord("V1", 100_000),
            ExternalPaymentAllocationSyncRecord("V2", 50_000),
        ),
    )
    unallocated = ExternalPaymentSyncRecord(
        external_id="R2",
        patient_external_id="C1",
        transaction_type="payment",
        amount_minor=25_000,
        currency="EGP",
        payment_method="cash",
        created_at=NOW + timedelta(minutes=1),
    )
    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            records=(allocated, unallocated),
            next_cursor="pay:1",
        ),
        now=NOW,
    )
    db.commit()

    assert result.created_count == 2
    assert db.query(PaymentTransaction).count() == 2
    assert db.query(PaymentAllocation).count() == 2
    allocated_tx = db.scalar(
        select(PaymentTransaction).where(PaymentTransaction.amount_minor == 150_000)
    )
    unallocated_tx = db.scalar(
        select(PaymentTransaction).where(PaymentTransaction.amount_minor == 25_000)
    )
    assert allocated_tx.appointment_id is None
    assert unallocated_tx.appointment_id is None
    assert db.get(Appointment, visit_1.id).payment_status == "paid"
    assert db.get(Appointment, visit_1.id).amount_paid_minor == 100_000
    assert db.get(Appointment, visit_2.id).payment_status == "partial"
    assert db.get(Appointment, visit_2.id).amount_paid_minor == 50_000

    replay = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            cursor="pay:1",
            records=(allocated, unallocated),
            next_cursor="pay:2",
        ),
        now=NOW + timedelta(minutes=2),
    )
    db.commit()
    assert replay.skipped_count == 2
    assert db.query(PaymentTransaction).count() == 2


def test_payment_missing_appointment_link_isolated_without_guessing(sync_db) -> None:
    db, workspace = sync_db
    patient = _insert_patient(db, workspace.id)
    _add_link(db, workspace.id, "patient", patient.id, "C1")

    good_unallocated = ExternalPaymentSyncRecord(
        external_id="UNALLOCATED",
        patient_external_id="C1",
        transaction_type="payment",
        amount_minor=20_000,
        currency="EGP",
        payment_method="cash",
        created_at=NOW,
    )
    bad_allocated = ExternalPaymentSyncRecord(
        external_id="MISSING-VISIT",
        patient_external_id="C1",
        transaction_type="payment",
        amount_minor=50_000,
        currency="EGP",
        payment_method="cash",
        created_at=NOW,
        allocations=(ExternalPaymentAllocationSyncRecord("DOES-NOT-EXIST", 50_000),),
    )
    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            records=(good_unallocated, bad_allocated),
            next_cursor="pay:next",
        ),
        now=NOW,
    )
    db.commit()

    assert result.status == "partial"
    assert result.created_count == 1
    assert result.failed_count == 1
    assert db.query(PaymentTransaction).count() == 1
    assert db.get(ClinicIntegrationSyncCheckpoint, (workspace.id, "payments")) is None
    failure = db.scalar(
        select(ClinicIntegrationSyncFailure).where(
            ClinicIntegrationSyncFailure.error_code == "missing_dependency_link"
        )
    )
    assert failure is not None


def test_external_financial_fact_is_immutable_after_linking(sync_db) -> None:
    db, workspace = sync_db
    patient = _insert_patient(db, workspace.id)
    _add_link(db, workspace.id, "patient", patient.id, "C1")
    original = ExternalPaymentSyncRecord(
        external_id="PAY-1",
        patient_external_id="C1",
        transaction_type="payment",
        amount_minor=10_000,
        currency="EGP",
        payment_method="cash",
        created_at=NOW,
    )
    apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            records=(original,),
            next_cursor="p1",
        ),
        now=NOW,
    )
    db.commit()

    changed = ExternalPaymentSyncRecord(
        **{**original.__dict__, "amount_minor": 11_000}
    )
    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            cursor="p1",
            records=(changed,),
            next_cursor="p2",
        ),
        now=NOW + timedelta(minutes=1),
    )
    db.commit()
    assert result.status == "failed"
    assert db.query(PaymentTransaction).count() == 1
    failure = db.scalar(
        select(ClinicIntegrationSyncFailure)
        .where(ClinicIntegrationSyncFailure.run_id == result.run_id)
    )
    assert failure.error_code == "immutable_financial_fact_changed"


def test_refund_requires_original_payment_and_respects_original_amount(sync_db) -> None:
    db, workspace = sync_db
    patient = _insert_patient(db, workspace.id)
    _add_link(db, workspace.id, "patient", patient.id, "C1")
    payment = ExternalPaymentSyncRecord(
        external_id="PAY-ORIG",
        patient_external_id="C1",
        transaction_type="payment",
        amount_minor=10_000,
        currency="EGP",
        payment_method="card",
        created_at=NOW,
    )
    apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            records=(payment,),
            next_cursor="c1",
        ),
        now=NOW,
    )
    db.commit()

    over_refund = ExternalPaymentSyncRecord(
        external_id="REF-1",
        patient_external_id="C1",
        transaction_type="refund",
        amount_minor=11_000,
        currency="EGP",
        payment_method="card",
        created_at=NOW + timedelta(minutes=1),
        reference_payment_external_id="PAY-ORIG",
    )
    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            cursor="c1",
            records=(over_refund,),
            next_cursor="c2",
        ),
        now=NOW + timedelta(minutes=2),
    )
    db.commit()
    assert result.status == "failed"
    assert db.query(PaymentTransaction).count() == 1
    failure = db.scalar(
        select(ClinicIntegrationSyncFailure).where(
            ClinicIntegrationSyncFailure.run_id == result.run_id
        )
    )
    assert failure.error_code == "refund_exceeds_payment"


def test_cursor_mismatch_refuses_out_of_order_page(sync_db) -> None:
    db, workspace = sync_db
    with pytest.raises(ClinicIntegrationSyncError, match="cursor"):
        apply_external_sync_page(
            db=db,
            workspace=workspace,
            page=ClinicSyncPage(
                domain=ClinicSyncDomain.PATIENTS,
                cursor="not-the-checkpoint",
                records=(),
                next_cursor="next",
            ),
            now=NOW,
        )


def test_structurally_transformed_rows_feed_canonical_sync_without_vendor_schema_in_core(sync_db) -> None:
    db, workspace = sync_db
    raw = {
        "clients": [
            {"Client ID": "C9", "First": "Mona", "Phone": "01033333333"},
        ],
        "visits": [
            {"Visit ID": "V9", "Client ID": "C9"},
        ],
        "receipts": [
            {"Receipt ID": "R9", "Client ID": "C9", "Total": 1500},
        ],
        "receipt_allocations": [
            {"Receipt ID": "R9", "Visit ID": "V9", "Amount": 1000},
        ],
    }
    patient_transform = StructuralTransformMapping(
        name="sync_patients",
        source_sheet="clients",
        source_alias="client",
        fields=[
            StructuralFieldMapping(name="External ID", source="client.Client ID"),
            StructuralFieldMapping(name="First Name", source="client.First"),
            StructuralFieldMapping(name="Phone", source="client.Phone"),
        ],
    )
    payment_transform = StructuralTransformMapping(
        name="sync_payment_allocations",
        source_sheet="receipt_allocations",
        source_alias="allocation",
        joins=[
            StructuralJoinMapping(
                sheet="receipts",
                alias="receipt",
                on=[StructuralJoinKey(left="allocation.Receipt ID", right="Receipt ID")],
                cardinality="one",
            )
        ],
        fields=[
            StructuralFieldMapping(name="Payment ID", source="receipt.Receipt ID"),
            StructuralFieldMapping(name="Patient ID", source="receipt.Client ID"),
            StructuralFieldMapping(name="Visit ID", source="allocation.Visit ID"),
            StructuralFieldMapping(name="Allocation", source="allocation.Amount"),
            StructuralFieldMapping(name="Transaction Total", source="receipt.Total"),
        ],
    )
    transformed, _ = apply_structural_transforms(
        raw, [patient_transform, payment_transform]
    )
    patient_row = transformed["sync_patients"][0]
    patient_result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(
                ExternalPatientSyncRecord(
                    external_id=patient_row["External ID"],
                    first_name=patient_row["First Name"],
                    phone=patient_row["Phone"],
                    source_updated_at=NOW,
                ),
            ),
            next_cursor="patients:done",
        ),
        now=NOW,
    )
    db.flush()
    assert patient_result.created_count == 1
    patient_link = db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace.id,
            ClinicIntegrationEntityLink.entity_type == "patient",
            ClinicIntegrationEntityLink.external_id == "C9",
        )
    )
    patient = db.get(Patient, UUID(patient_link.canonical_id))
    appointment = _insert_appointment(
        db, workspace.id, patient.id, external_id="V9", price_minor=100_000
    )

    row = transformed["sync_payment_allocations"][0]
    payment_result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PAYMENTS,
            records=(
                ExternalPaymentSyncRecord(
                    external_id=row["Payment ID"],
                    patient_external_id=row["Patient ID"],
                    transaction_type="payment",
                    amount_minor=int(row["Transaction Total"]) * 100,
                    currency="EGP",
                    payment_method="card",
                    created_at=NOW,
                    allocations=(
                        ExternalPaymentAllocationSyncRecord(
                            appointment_external_id=row["Visit ID"],
                            amount_minor=int(row["Allocation"]) * 100,
                        ),
                    ),
                ),
            ),
            next_cursor="payments:done",
        ),
        now=NOW,
    )
    db.commit()
    assert payment_result.created_count == 1
    transaction = db.scalar(select(PaymentTransaction))
    assert transaction.amount_minor == 150_000
    assert transaction.appointment_id is None
    allocation = db.scalar(select(PaymentAllocation))
    assert allocation.appointment_id == appointment.id
    assert allocation.amount_minor == 100_000


def test_phase62c_migration_compiles_for_postgresql() -> None:
    import importlib.util
    import io

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    backend = Path(__file__).resolve().parent.parent
    migration_path = backend / "alembic/versions/0032_external_sync_engine.py"
    spec = importlib.util.spec_from_file_location("tia_migration_0032", migration_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    module.op = Operations(context)
    module.upgrade()
    sql = buffer.getvalue()
    assert "CREATE TABLE clinic_integration_sync_runs" in sql
    assert "CREATE TABLE clinic_integration_sync_checkpoints" in sql
    assert "CREATE TABLE clinic_integration_sync_failures" in sql


def test_phase62c_migration_and_runtime_contract_are_incremental() -> None:
    backend = Path(__file__).resolve().parent.parent
    migration = (backend / "alembic/versions/0032_external_sync_engine.py").read_text(
        encoding="utf-8"
    )
    source = (backend / "app/services/clinic_integration_sync.py").read_text(
        encoding="utf-8"
    ).lower()
    contract = (backend / "app/integrations/clinic/sync_contract.py").read_text(
        encoding="utf-8"
    ).lower()
    assert 'revision: str = "0032_external_sync_engine"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0031_payment_allocations"' in migration
    assert "clinic_integration_sync_runs" in migration
    assert "clinic_integration_sync_checkpoints" in migration
    assert "clinic_integration_sync_failures" in migration
    assert "external_id_digest" in migration
    assert 'sa.column("external_id"' not in migration.lower()
    assert "begin_nested" in source
    assert "with_for_update" in source
    assert "clinicintegrationentitylink" in source.replace("_", "")
    assert "eval(" not in source
    assert "re.compile" not in source
    assert "sqlalchemy" not in contract
    assert "app.models." not in contract
