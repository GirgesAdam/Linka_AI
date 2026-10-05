from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    normalize_authority_policy,
    require_tia_workspace_domain_write,
)
from app.integrations.clinic.sync_contract import (
    ClinicSyncDomain,
    ClinicSyncPage,
    ExternalAppointmentSyncRecord,
)
from app.integrations.clinic.tia_database import TiaDatabaseClinicAdapter
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.clinic_integration import ClinicIntegration, ClinicIntegrationEntityLink
from app.models.clinic_integration_sync import ClinicIntegrationSyncFailure
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.services import clinic_integration_sync as sync_service
from app.services.booking import BookingRuleError
from app.services.clinic_integration_sync import (
    apply_external_sync_page,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
START = NOW + timedelta(days=1)


def _create_schema(engine) -> None:
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
            phone_normalized VARCHAR(40), gender VARCHAR(32), birth_date DATE,
            source_created_at DATETIME, preferred_language VARCHAR(10), preferred_branch_id CHAR(32), source VARCHAR(32),
            source_detail VARCHAR(200), status VARCHAR(20), marketing_consent BOOLEAN DEFAULT 0,
            marketing_consent_at DATETIME, whatsapp_opt_in BOOLEAN DEFAULT 0,
            whatsapp_opt_in_at DATETIME, whatsapp_opt_in_source VARCHAR(32), last_contact_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE branches (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), name VARCHAR(200), code VARCHAR(50),
            phone VARCHAR(40), email VARCHAR(320), address_line1 VARCHAR(300), address_line2 VARCHAR(300),
            city VARCHAR(120), state VARCHAR(120), country_code VARCHAR(2), timezone VARCHAR(64),
            is_active BOOLEAN, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE services (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), name VARCHAR(200), slug VARCHAR(160),
            category VARCHAR(120), operational_category VARCHAR(20) DEFAULT 'dermatology', description TEXT, duration_minutes INTEGER,
            buffer_before_minutes INTEGER, buffer_after_minutes INTEGER, price_minor INTEGER,
            currency VARCHAR(3), requires_medical_review BOOLEAN, requires_laser_device BOOLEAN DEFAULT 0, is_active BOOLEAN,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE doctors (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), staff_id CHAR(32),
            doctor_type VARCHAR(16) DEFAULT 'regular',
            specialization VARCHAR(200), license_number VARCHAR(120), bio VARCHAR(2000),
            booking_enabled BOOLEAN, is_active BOOLEAN,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE clinic_integration_entity_links (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), entity_type VARCHAR(32),
            canonical_id VARCHAR(255), external_id VARCHAR(512), metadata TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE UNIQUE INDEX uq_ext_link ON clinic_integration_entity_links(workspace_id, entity_type, external_id)",
        "CREATE UNIQUE INDEX uq_can_link ON clinic_integration_entity_links(workspace_id, entity_type, canonical_id)",
        """
        CREATE TABLE clinic_integration_sync_runs (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), domain VARCHAR(24), status VARCHAR(20),
            cursor_before VARCHAR(512), cursor_after VARCHAR(512), source_revision VARCHAR(255),
            processed_count INTEGER DEFAULT 0, created_count INTEGER DEFAULT 0, updated_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME
        )
        """,
        """
        CREATE TABLE clinic_integration_sync_checkpoints (
            workspace_id CHAR(32), domain VARCHAR(24), cursor VARCHAR(512), source_revision VARCHAR(255),
            last_success_at DATETIME, last_run_id CHAR(32), created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(workspace_id, domain)
        )
        """,
        """
        CREATE TABLE clinic_integration_sync_failures (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), run_id CHAR(32), domain VARCHAR(24),
            external_id_digest VARCHAR(64), error_code VARCHAR(80), message VARCHAR(300), retryable BOOLEAN,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE appointments (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), patient_id CHAR(32), branch_id CHAR(32),
            doctor_id CHAR(32), doctor_assignment_known BOOLEAN DEFAULT 1, service_id CHAR(32), patient_package_id CHAR(32), visit_group_id CHAR(32), lead_id CHAR(32), created_by_user_id CHAR(32),
            rescheduled_from_appointment_id CHAR(32), status VARCHAR(20), source VARCHAR(20),
            start_at DATETIME, end_at DATETIME, busy_start_at DATETIME, busy_end_at DATETIME,
            duration_minutes INTEGER, price_minor INTEGER, discount_minor INTEGER DEFAULT 0, is_quick_booking BOOLEAN DEFAULT 0, laser_pulses_used INTEGER, currency VARCHAR(3), laser_device_key VARCHAR(40), laser_device_name VARCHAR(120), payment_status VARCHAR(16),
            amount_paid_minor INTEGER, payment_method VARCHAR(20), billing_context VARCHAR(24) DEFAULT 'standard', package_external_id VARCHAR(128), customer_note TEXT, cancellation_reason TEXT,
            idempotency_key VARCHAR(128), confirmed_at DATETIME, cancelled_at DATETIME, completed_at DATETIME,
            no_show_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE UNIQUE INDEX uq_appt_idem ON appointments(workspace_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
        """
        CREATE TABLE payment_transactions (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), appointment_id CHAR(32), origin_appointment_id CHAR(32),
            patient_id CHAR(32), created_by_user_id CHAR(32), reference_transaction_id CHAR(32), patient_package_id CHAR(32), patient_pulse_pack_id CHAR(32), transaction_type VARCHAR(16),
            amount_minor INTEGER, currency VARCHAR(3), payment_method VARCHAR(24), source VARCHAR(24),
            external_reference VARCHAR(128), reason TEXT, idempotency_key VARCHAR(128), created_at DATETIME
        )
        """,
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
    ]
    with engine.begin() as connection:
        for statement in ddl:
            connection.exec_driver_sql(statement)


@pytest.fixture()
def db_and_workspace(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _create_schema(engine)
    workspace_id = uuid4()
    policy = (
        '{"patients":{"owner":"external","fields":{}},'
        '"payments":{"owner":"external","fields":{}},'
        '"appointments":{"owner":"external","fields":{}}}'
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO clinic_integrations "
                "(workspace_id, mode, adapter_key, status, config, authority_policy) "
                "VALUES (:workspace_id, 'external_api', 'prototype_external', 'active', '{}', :policy)"
            ),
            {"workspace_id": workspace_id.hex, "policy": policy},
        )
    monkeypatch.setattr(sync_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync_service, "add_appointment_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync_service, "cancel_pending_appointment_jobs", lambda *args, **kwargs: None)
    with Session(engine) as db:
        patient = Patient(
            workspace_id=workspace_id,
            first_name="Sara",
            phone="01001234567",
            phone_normalized="+201001234567",
            preferred_language="ar",
            source="other",
            status="active",
        )
        branch = Branch(
            workspace_id=workspace_id,
            name="Main",
            code="main",
            country_code="EG",
            is_active=True,
        )
        service = Service(
            workspace_id=workspace_id,
            name="Laser",
            slug="laser",
            duration_minutes=60,
            buffer_before_minutes=10,
            buffer_after_minutes=5,
            price_minor=100_000,
            currency="EGP",
            requires_medical_review=False,
            is_active=True,
        )
        doctor = Doctor(
            workspace_id=workspace_id,
            staff_id=uuid4(),
            booking_enabled=True,
            is_active=True,
        )
        db.add_all([patient, branch, service, doctor])
        db.flush()
        for entity_type, entity, external_id in (
            ("patient", patient, "P-1"),
            ("branch", branch, "B-1"),
            ("service", service, "S-1"),
            ("doctor", doctor, "D-1"),
        ):
            db.add(
                ClinicIntegrationEntityLink(
                    workspace_id=workspace_id,
                    entity_type=entity_type,
                    canonical_id=str(entity.id),
                    external_id=external_id,
                    metadata_json={},
                )
            )
        db.flush()
        yield db, SimpleNamespace(id=workspace_id, timezone="Africa/Cairo"), patient


def _record(
    *,
    external_id: str = "A-1",
    status: str = "confirmed",
    start_at: datetime = START,
    end_at: datetime | None = None,
    status_at: datetime | None = NOW,
    source_updated_at: datetime | None = NOW,
    patient_external_id: str = "P-1",
) -> ExternalAppointmentSyncRecord:
    return ExternalAppointmentSyncRecord(
        external_id=external_id,
        patient_external_id=patient_external_id,
        branch_external_id="B-1",
        service_external_id="S-1",
        doctor_external_id="D-1",
        status=status,
        start_at=start_at,
        end_at=end_at or start_at + timedelta(hours=1),
        status_at=status_at,
        source_updated_at=source_updated_at,
    )


def _sync(db: Session, workspace, record: ExternalAppointmentSyncRecord, *, cursor=None, next_cursor=None, now=NOW):
    return apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.APPOINTMENTS,
            records=(record,),
            cursor=cursor,
            next_cursor=next_cursor,
        ),
        now=now,
    )


def test_external_appointment_create_and_replay_are_idempotent(db_and_workspace) -> None:
    db, workspace, patient = db_and_workspace
    first = _sync(db, workspace, _record(), next_cursor="c1")
    assert first.status == "succeeded"
    assert first.created_count == 1

    link = db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace.id,
            ClinicIntegrationEntityLink.entity_type == "appointment",
            ClinicIntegrationEntityLink.external_id == "A-1",
        )
    )
    appointment = db.get(Appointment, UUID(link.canonical_id))
    assert appointment.patient_id == patient.id
    assert appointment.status == "confirmed"
    assert appointment.price_minor == 100_000
    assert appointment.busy_start_at == START.replace(tzinfo=None) - timedelta(minutes=10)

    replay = _sync(db, workspace, _record(), cursor="c1", next_cursor="c2")
    assert replay.status == "succeeded"
    assert replay.skipped_count == 1
    assert db.scalar(select(text("count(*)")).select_from(Appointment.__table__)) == 1


def test_external_reschedule_creates_replacement_moves_link_and_payment_allocation(db_and_workspace) -> None:
    db, workspace, patient = db_and_workspace
    _sync(db, workspace, _record(), next_cursor="c1")
    link = db.scalar(select(ClinicIntegrationEntityLink).where(ClinicIntegrationEntityLink.entity_type == "appointment"))
    old = db.get(Appointment, UUID(link.canonical_id))
    payment = PaymentTransaction(
        workspace_id=workspace.id,
        appointment_id=old.id,
        origin_appointment_id=old.id,
        patient_id=patient.id,
        transaction_type="payment",
        amount_minor=50_000,
        currency="EGP",
        payment_method="cash",
        source="integration",
        idempotency_key="test-payment",
        created_at=NOW,
    )
    db.add(payment)
    db.flush()
    allocation = PaymentAllocation(
        workspace_id=workspace.id,
        transaction_id=payment.id,
        appointment_id=old.id,
        amount_minor=50_000,
        created_at=NOW,
    )
    db.add(allocation)
    db.flush()

    new_start = START + timedelta(days=1)
    result = _sync(
        db,
        workspace,
        _record(start_at=new_start, status_at=NOW + timedelta(minutes=5), source_updated_at=NOW + timedelta(minutes=5)),
        cursor="c1",
        next_cursor="c2",
        now=NOW + timedelta(minutes=5),
    )
    assert result.updated_count == 1
    db.refresh(link)
    replacement = db.get(Appointment, UUID(link.canonical_id))
    db.refresh(old)
    db.refresh(allocation)
    db.refresh(payment)
    assert replacement.id != old.id
    assert replacement.rescheduled_from_appointment_id == old.id
    assert replacement.start_at == new_start.replace(tzinfo=None)
    assert old.status == "rescheduled"
    assert allocation.appointment_id == replacement.id
    assert payment.appointment_id == replacement.id


def test_external_status_update_completes_without_changing_schedule(db_and_workspace) -> None:
    db, workspace, _ = db_and_workspace
    _sync(db, workspace, _record(), next_cursor="c1")
    completed_at = START + timedelta(hours=1, minutes=10)
    result = _sync(
        db,
        workspace,
        _record(
            status="completed",
            status_at=completed_at,
            source_updated_at=NOW + timedelta(minutes=10),
        ),
        cursor="c1",
        next_cursor="c2",
        now=NOW + timedelta(minutes=10),
    )
    assert result.updated_count == 1
    link = db.scalar(select(ClinicIntegrationEntityLink).where(ClinicIntegrationEntityLink.entity_type == "appointment"))
    appointment = db.get(Appointment, UUID(link.canonical_id))
    assert appointment.status == "completed"
    assert appointment.completed_at == completed_at.replace(tzinfo=None)


def test_external_appointment_conflict_fails_closed_after_local_mutation(db_and_workspace) -> None:
    db, workspace, _ = db_and_workspace
    _sync(db, workspace, _record(), next_cursor="c1")
    link = db.scalar(select(ClinicIntegrationEntityLink).where(ClinicIntegrationEntityLink.entity_type == "appointment"))
    appointment = db.get(Appointment, UUID(link.canonical_id))
    appointment.price_minor = 123_456
    db.flush()

    result = _sync(
        db,
        workspace,
        _record(source_updated_at=NOW + timedelta(minutes=5)),
        cursor="c1",
        next_cursor="c2",
        now=NOW + timedelta(minutes=5),
    )
    assert result.status == "failed"
    failure = db.scalar(select(ClinicIntegrationSyncFailure).order_by(ClinicIntegrationSyncFailure.created_at.desc()))
    assert failure.error_code == "source_authority_conflict"
    assert appointment.price_minor == 123_456


def test_terminal_status_requires_explicit_status_time(db_and_workspace) -> None:
    db, workspace, _ = db_and_workspace
    result = _sync(db, workspace, _record(status="completed", status_at=None))
    assert result.status == "failed"
    failure = db.scalar(select(ClinicIntegrationSyncFailure))
    assert failure.error_code == "missing_appointment_status_time"
    assert failure.retryable is False


def test_missing_dependency_does_not_advance_checkpoint(db_and_workspace) -> None:
    db, workspace, _ = db_and_workspace
    bad = ExternalAppointmentSyncRecord(
        external_id="A-MISSING",
        patient_external_id="P-1",
        branch_external_id="B-MISSING",
        service_external_id="S-1",
        doctor_external_id="D-1",
        status="pending",
        start_at=START,
        end_at=START + timedelta(hours=1),
        source_updated_at=NOW,
    )
    result = _sync(db, workspace, bad, next_cursor="c1")
    assert result.status == "failed"
    assert result.cursor_after is None
    failure = db.scalar(select(ClinicIntegrationSyncFailure))
    assert failure.error_code == "missing_dependency_link"
    assert failure.retryable is True


def test_linked_appointment_cannot_change_patient(db_and_workspace) -> None:
    db, workspace, _ = db_and_workspace
    _sync(db, workspace, _record(), next_cursor="c1")
    other = Patient(
        workspace_id=workspace.id,
        first_name="Mona",
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add(other)
    db.flush()
    db.add(
        ClinicIntegrationEntityLink(
            workspace_id=workspace.id,
            entity_type="patient",
            canonical_id=str(other.id),
            external_id="P-2",
            metadata_json={},
        )
    )
    db.flush()
    result = _sync(
        db,
        workspace,
        _record(patient_external_id="P-2", source_updated_at=NOW + timedelta(minutes=5)),
        cursor="c1",
        next_cursor="c2",
    )
    assert result.status == "failed"
    failure = db.scalar(select(ClinicIntegrationSyncFailure).order_by(ClinicIntegrationSyncFailure.created_at.desc()))
    assert failure.error_code == "source_authority_conflict" or failure.error_code == "appointment_patient_conflict"


def test_appointment_authority_is_opt_in_and_external_mode_only(db_and_workspace) -> None:
    db, workspace, _ = db_and_workspace
    default = normalize_authority_policy({}, mode="external_api")
    assert default["appointments"]["owner"] == "tia"
    enabled = normalize_authority_policy(
        {
            "patients": {"owner": "external", "fields": {}},
            "payments": {"owner": "external", "fields": {}},
            "appointments": {"owner": "external", "fields": {}},
        },
        mode="external_api",
    )
    assert enabled["appointments"]["owner"] == "external"
    with pytest.raises(ClinicIntegrationAuthorityError, match="external_api or hybrid"):
        normalize_authority_policy(enabled, mode="imported")

    integration = db.get(ClinicIntegration, workspace.id)
    integration.authority_policy_json = {
        "patients": {"owner": "external", "fields": {}},
        "payments": {"owner": "external", "fields": {}},
        "appointments": {"owner": "external", "fields": {}},
    }
    with pytest.raises(ClinicIntegrationAuthorityError, match="not authoritative"):
        require_tia_workspace_domain_write(db, workspace_id=workspace.id, domain="appointments")



def test_local_write_guards_cover_dashboard_and_native_adapter(db_and_workspace) -> None:
    db, workspace, _ = db_and_workspace
    adapter = TiaDatabaseClinicAdapter(db=db, workspace=workspace)
    with pytest.raises(BookingRuleError, match="not authoritative"):
        adapter._require_local_appointment_write()

    backend = Path(__file__).resolve().parent.parent
    booking_source = (backend / "app/api/routes/booking.py").read_text(encoding="utf-8")
    assert booking_source.count("require_local_appointment_write(db, access.workspace.id)") == 9
def test_migration_0035_extends_sync_domain_checks_and_head() -> None:
    backend = Path(__file__).resolve().parent.parent
    migration = (backend / "alembic/versions/0035_appointment_sync.py").read_text(encoding="utf-8")
    readiness = (backend / "app/services/operational_readiness.py").read_text(encoding="utf-8")
    assert 'revision: str = "0035_appointment_sync"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0034_drop_customer_email"' in migration
    assert "'patients', 'payments', 'appointments'" in migration
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness


def _load_appointment_sync_migration_module():
    import importlib.util

    backend = Path(__file__).resolve().parent.parent
    path = backend / "alembic/versions/0035_appointment_sync.py"
    spec = importlib.util.spec_from_file_location("migration_0035_appointment_sync_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0035_discovers_live_legacy_domain_check_name(monkeypatch) -> None:
    migration = _load_appointment_sync_migration_module()

    class FakeInspector:
        def get_check_constraints(self, table_name: str):
            assert table_name == "clinic_integration_sync_runs"
            return [
                {"name": "some_status_check", "sqltext": "status IN ('running', 'failed')"},
                {
                    "name": "ck_clinic_integration_sync_runs_ck_clinic_integration_s_7aa7",
                    "sqltext": "domain::text = ANY (ARRAY['patients'::text, 'payments'::text])",
                },
            ]

    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: FakeInspector())

    assert migration._live_domain_check_name("clinic_integration_sync_runs") == (
        "ck_clinic_integration_sync_runs_ck_clinic_integration_s_7aa7"
    )


def test_migration_0035_legacy_offline_names_match_0032_postgresql_compiler() -> None:
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    from app.database.base import NAMING_CONVENTION

    migration = _load_appointment_sync_migration_module()
    for table_name, expected_name in migration._LEGACY_DOMAIN_CHECK_NAMES.items():
        metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
        table = sa.Table(
            table_name,
            metadata,
            sa.Column("domain", sa.String(24), nullable=False),
            sa.CheckConstraint(
                "domain IN ('patients', 'payments')",
                name=f"ck_{table_name}_domain_valid",
            ),
        )
        sql = str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect()))
        assert expected_name in sql
        assert len(expected_name) <= 63
