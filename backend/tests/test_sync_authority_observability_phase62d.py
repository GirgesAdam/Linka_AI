from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import (
    ClinicIntegrationAuthorityError,
    default_authority_policy_for_mode,
    require_tia_patient_fields_writable,
)
from app.integrations.clinic.sync_contract import (
    ClinicSyncDomain,
    ClinicSyncPage,
    ExternalPatientSyncRecord,
)
from app.models.clinic_integration import ClinicIntegration, ClinicIntegrationEntityLink
from app.models.clinic_integration_sync import ClinicIntegrationSyncFailure
from app.models.patient import Patient
from app.services import clinic_integration_sync as sync_service
from app.services.clinic_integration_runtime import build_clinic_integration_runtime
from app.services.clinic_integration_sync import (
    ClinicIntegrationSyncError,
    apply_external_sync_page,
)
from app.services.payments import PaymentOperationError, record_payment

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)


def _schema(engine) -> None:
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
        "CREATE UNIQUE INDEX uq_phase62d_patient_phone ON patients(workspace_id, phone_normalized) WHERE phone_normalized IS NOT NULL",
        """
        CREATE TABLE clinic_integration_entity_links (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), entity_type VARCHAR(32),
            canonical_id VARCHAR(255), external_id VARCHAR(512), metadata TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE UNIQUE INDEX uq_phase62d_link_external ON clinic_integration_entity_links(workspace_id, entity_type, external_id)",
        "CREATE UNIQUE INDEX uq_phase62d_link_canonical ON clinic_integration_entity_links(workspace_id, entity_type, canonical_id)",
        """
        CREATE TABLE clinic_integration_sync_schedules (
            workspace_id CHAR(32) PRIMARY KEY, enabled BOOLEAN DEFAULT 0,
            interval_minutes INTEGER DEFAULT 15, next_run_at DATETIME, locked_at DATETIME,
            attempts INTEGER DEFAULT 0, last_error VARCHAR(300), last_completed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
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
            branch_id CHAR(32), doctor_id CHAR(32), service_id CHAR(32), lead_id CHAR(32),
            created_by_user_id CHAR(32), rescheduled_from_appointment_id CHAR(32),
            status VARCHAR(20), source VARCHAR(20), start_at DATETIME, end_at DATETIME,
            busy_start_at DATETIME, busy_end_at DATETIME, duration_minutes INTEGER,
            price_minor INTEGER, currency VARCHAR(3), payment_status VARCHAR(16),
            amount_paid_minor INTEGER, payment_method VARCHAR(20), customer_note TEXT,
            cancellation_reason TEXT, idempotency_key VARCHAR(128), confirmed_at DATETIME,
            cancelled_at DATETIME, completed_at DATETIME, no_show_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE payment_transactions (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), appointment_id CHAR(32),
            origin_appointment_id CHAR(32), patient_id CHAR(32), created_by_user_id CHAR(32),
            reference_transaction_id CHAR(32), transaction_type VARCHAR(16), amount_minor INTEGER,
            currency VARCHAR(3), payment_method VARCHAR(24), source VARCHAR(24),
            external_reference VARCHAR(128), reason TEXT, idempotency_key VARCHAR(128),
            created_at DATETIME
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
    _schema(engine)
    workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO clinic_integrations "
                "(workspace_id, mode, adapter_key, status, config, authority_policy) "
                "VALUES (:workspace_id, 'external_api', 'prototype_external', 'active', '{}', :policy)"
            ),
            {
                "workspace_id": workspace_id.hex,
                "policy": '{"patients":{"owner":"external","fields":{}},"payments":{"owner":"external","fields":{}},"appointments":{"owner":"tia","fields":{}}}',
            },
        )
    monkeypatch.setattr(sync_service, "record_activity_event", lambda *args, **kwargs: None)
    with Session(engine) as db:
        yield db, SimpleNamespace(id=workspace_id, timezone="Africa/Cairo")


def _patient_record(external_id: str, *, name: str, language: str = "ar", at=NOW):
    return ExternalPatientSyncRecord(
        external_id=external_id,
        first_name=name,
        phone="+201001234567",
        status="active",
        preferred_language=language,
        source="other",
        source_updated_at=at,
    )


def test_authority_defaults_are_conservative_by_mode() -> None:
    external = default_authority_policy_for_mode("external_api")
    native = default_authority_policy_for_mode("tia_native")
    assert external["patients"]["owner"] == "external"
    assert external["payments"]["owner"] == "external"
    assert external["appointments"]["owner"] == "tia"
    assert native["patients"]["owner"] == "tia"
    assert native["payments"]["owner"] == "tia"
    assert native["appointments"]["owner"] == "tia"


def test_patient_field_override_preserves_tia_owned_field(db_and_workspace) -> None:
    db, workspace = db_and_workspace
    integration = db.get(ClinicIntegration, workspace.id)
    integration.authority_policy_json = {
        "patients": {"owner": "external", "fields": {"preferred_language": "tia"}},
        "payments": {"owner": "external", "fields": {}},
        "appointments": {"owner": "tia", "fields": {}},
    }

    first = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(_patient_record("P-1", name="Sara"),),
            next_cursor="c1",
        ),
        now=NOW,
    )
    assert first.created_count == 1
    patient = db.scalar(select(Patient).where(Patient.workspace_id == workspace.id))
    patient.preferred_language = "en"
    db.flush()

    second = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            cursor="c1",
            records=(
                _patient_record(
                    "P-1",
                    name="Sara External",
                    language="fr",
                    at=NOW + timedelta(minutes=5),
                ),
            ),
            next_cursor="c2",
        ),
        now=NOW + timedelta(minutes=5),
    )
    assert second.updated_count == 1
    assert patient.first_name == "Sara External"
    assert patient.preferred_language == "en"


def test_conflicting_local_edit_on_external_owned_field_fails_closed(db_and_workspace) -> None:
    db, workspace = db_and_workspace
    first = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(_patient_record("P-2", name="Original"),),
            next_cursor="c1",
        ),
        now=NOW,
    )
    assert first.created_count == 1
    patient = db.scalar(select(Patient).where(Patient.workspace_id == workspace.id))
    patient.first_name = "Local Edit"
    db.flush()

    conflict = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            cursor="c1",
            records=(
                _patient_record(
                    "P-2", name="External Edit", at=NOW + timedelta(minutes=10)
                ),
            ),
            next_cursor="c2",
        ),
        now=NOW + timedelta(minutes=10),
    )
    assert conflict.status == "failed"
    assert conflict.failed_count == 1
    assert patient.first_name == "Local Edit"
    failure = db.scalar(select(ClinicIntegrationSyncFailure).order_by(ClinicIntegrationSyncFailure.created_at.desc()))
    assert failure.error_code == "source_authority_conflict"
    assert failure.retryable is False


def test_tia_owned_patient_domain_rejects_external_sync(db_and_workspace) -> None:
    db, workspace = db_and_workspace
    integration = db.get(ClinicIntegration, workspace.id)
    integration.authority_policy_json = default_authority_policy_for_mode("tia_native")
    with pytest.raises(ClinicIntegrationSyncError, match="not authoritative"):
        apply_external_sync_page(
            db=db,
            workspace=workspace,
            page=ClinicSyncPage(
                domain=ClinicSyncDomain.PATIENTS,
                records=(_patient_record("P-3", name="Blocked"),),
            ),
            now=NOW,
        )


def test_tia_patient_edit_guard_blocks_external_fields_but_not_local_fields(db_and_workspace) -> None:
    db, workspace = db_and_workspace
    patient = Patient(
        workspace_id=workspace.id,
        first_name="Sara",
        phone="01001234567",
        phone_normalized="+201001234567",
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add(patient)
    db.flush()
    db.add(
        ClinicIntegrationEntityLink(
            workspace_id=workspace.id,
            entity_type="patient",
            canonical_id=str(patient.id),
            external_id="P-4",
            metadata_json={},
        )
    )
    db.flush()

    require_tia_patient_fields_writable(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        fields={"marketing_consent"},
    )
    with pytest.raises(ClinicIntegrationAuthorityError, match="first_name"):
        require_tia_patient_fields_writable(
            db,
            workspace_id=workspace.id,
            patient_id=patient.id,
            fields={"first_name", "marketing_consent"},
        )


def test_manual_payment_is_blocked_when_external_system_owns_payments(db_and_workspace) -> None:
    db, workspace = db_and_workspace
    with pytest.raises(PaymentOperationError, match="not authoritative"):
        record_payment(
            db,
            workspace_id=workspace.id,
            appointment_id=uuid4(),
            amount_minor=1000,
            payment_method="cash",
            created_by_user_id=uuid4(),
        )


def test_runtime_exposes_safe_sync_health_and_not_raw_cursor(db_and_workspace) -> None:
    db, workspace = db_and_workspace
    first = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(_patient_record("P-5", name="Sara"),),
            next_cursor="opaque-cursor-containing-source-id-123",
            source_revision="rev-1",
        ),
        now=NOW,
    )
    assert first.status == "succeeded"

    second = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            cursor="opaque-cursor-containing-source-id-123",
            records=(
                ExternalPatientSyncRecord(
                    external_id="P-BAD",
                    first_name="Bad",
                    status="not-canonical",
                    source_updated_at=NOW + timedelta(minutes=1),
                ),
            ),
            next_cursor="should-not-advance",
            source_revision="rev-2",
        ),
        now=NOW + timedelta(minutes=1),
    )
    assert second.status == "failed"

    runtime = build_clinic_integration_runtime(db, workspace)
    assert {item.domain for item in runtime.sync_domains} == {"patients", "payments", "appointments"}
    patients = next(item for item in runtime.sync_domains if item.domain == "patients")
    assert patients.authority_owner == "external"
    assert patients.checkpoint_present is True
    assert patients.cursor_digest == hashlib.sha256(
        b"opaque-cursor-containing-source-id-123"
    ).hexdigest()[:16]
    assert patients.source_revision == "rev-1"
    assert patients.last_run_status == "failed"
    assert patients.failed_count == 1
    assert patients.latest_error_code == "invalid_patient_status"
    serialized = runtime.model_dump_json()
    assert "opaque-cursor-containing-source-id-123" not in serialized
    assert "P-BAD" not in serialized



def test_appointment_external_authority_requires_continuous_external_mode() -> None:
    from app.integrations.clinic.authority import normalize_authority_policy

    policy = default_authority_policy_for_mode("external_api")
    policy["appointments"]["owner"] = "external"
    normalized = normalize_authority_policy(policy, mode="external_api")
    assert normalized["appointments"]["owner"] == "external"

    with pytest.raises(ClinicIntegrationAuthorityError, match="external_api or hybrid"):
        normalize_authority_policy(policy, mode="imported")

def test_migration_adds_authority_column_and_backfills_by_mode() -> None:
    backend = Path(__file__).resolve().parent.parent
    migration = (backend / "alembic/versions/0033_sync_authority_observability.py").read_text(
        encoding="utf-8"
    )
    assert 'revision: str = "0033_sync_authority"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0032_external_sync_engine"' in migration
    assert '"authority_policy"' in migration
    assert "external_api', 'hybrid" in migration
    assert '"appointments":{"owner":"tia"' in migration


def test_external_patient_sync_hydrates_historical_demographics(db_and_workspace) -> None:
    from datetime import date

    db, workspace = db_and_workspace
    original_created_at = datetime(2021, 6, 5, 8, 30, tzinfo=UTC)
    result = apply_external_sync_page(
        db=db,
        workspace=workspace,
        page=ClinicSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            records=(
                ExternalPatientSyncRecord(
                    external_id="P-HISTORY-1",
                    first_name="Mona",
                    phone="+201001234567",
                    gender="female",
                    birth_date=date(1990, 4, 3),
                    source_created_at=original_created_at,
                    source_updated_at=NOW,
                ),
            ),
            next_cursor="history-1",
        ),
        now=NOW,
    )
    assert result.created_count == 1
    patient = db.scalar(select(Patient).where(Patient.workspace_id == workspace.id))
    assert patient.gender == "female"
    assert patient.birth_date == date(1990, 4, 3)
    assert patient.source_created_at.replace(tzinfo=UTC) == original_created_at
