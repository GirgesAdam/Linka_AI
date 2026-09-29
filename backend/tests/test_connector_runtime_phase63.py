from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.integrations.clinic.prototype_external import PrototypeExternalClinicAdapter
from app.integrations.clinic.sync_contract import (
    ClinicSyncDomain,
    ClinicSyncFetchRequest,
    ClinicSyncPage,
    ClinicSyncSource,
    ExternalPatientSyncRecord,
)
from app.models.clinic_integration_sync import (
    ClinicIntegrationSyncCheckpoint,
    ClinicIntegrationSyncFailure,
    ClinicIntegrationSyncSchedule,
)
from app.models.patient import Patient
from app.schemas.clinic_integration import ClinicSyncScheduleUpsert
from app.services import clinic_integration_sync as sync_service
from app.services import clinic_integration_sync_runtime as runtime

NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)


def _schema(engine) -> None:
    statements = [
        """
        CREATE TABLE clinic_integrations (
            workspace_id CHAR(32) PRIMARY KEY, mode VARCHAR(32), adapter_key VARCHAR(80),
            status VARCHAR(24), external_clinic_id VARCHAR(255), secret_ref VARCHAR(512),
            config TEXT DEFAULT '{}', authority_policy TEXT DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE clinic_integration_sync_schedules (
            workspace_id CHAR(32) PRIMARY KEY, enabled BOOLEAN DEFAULT 0,
            interval_minutes INTEGER DEFAULT 15, next_run_at DATETIME, locked_at DATETIME,
            attempts INTEGER DEFAULT 0, last_error VARCHAR(300), last_completed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        "CREATE UNIQUE INDEX uq_phase63_patient_phone ON patients(workspace_id, phone_normalized) WHERE phone_normalized IS NOT NULL",
        """
        CREATE TABLE clinic_integration_entity_links (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), entity_type VARCHAR(32),
            canonical_id VARCHAR(255), external_id VARCHAR(512), metadata TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE UNIQUE INDEX uq_phase63_link_external ON clinic_integration_entity_links(workspace_id, entity_type, external_id)",
        "CREATE UNIQUE INDEX uq_phase63_link_canonical ON clinic_integration_entity_links(workspace_id, entity_type, canonical_id)",
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
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


@pytest.fixture()
def db_workspace(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _schema(engine)
    workspace_id = uuid4()
    policy = {
        "patients": {"owner": "external", "fields": {}},
        "payments": {"owner": "tia", "fields": {}},
        "appointments": {"owner": "tia", "fields": {}},
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO clinic_integrations "
                "(workspace_id, mode, adapter_key, status, config, authority_policy) "
                "VALUES (:workspace_id, 'external_api', 'prototype_external', 'active', '{}', :policy)"
            ),
            {"workspace_id": workspace_id.hex, "policy": json.dumps(policy)},
        )
    monkeypatch.setattr(sync_service, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "record_activity_event", lambda *args, **kwargs: None)
    with Session(engine) as db:
        yield db, SimpleNamespace(id=workspace_id, timezone="Africa/Cairo", is_demo=False)


class OnePatientSource:
    sync_domains = frozenset({ClinicSyncDomain.PATIENTS})
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def fetch_sync_page(self, request: ClinicSyncFetchRequest) -> ClinicSyncPage:
        self.calls.append(request.cursor)
        return ClinicSyncPage(
            domain=request.domain,
            cursor=request.cursor,
            next_cursor=None,
            source_revision="rev-1",
            records=(
                ExternalPatientSyncRecord(
                    external_id="P-1",
                    first_name="Sara",
                    phone="+201001234567",
                    source_updated_at=NOW,
                ),
            ),
        )


class TwoPagePatientSource:
    sync_domains = frozenset({ClinicSyncDomain.PATIENTS})

    def fetch_sync_page(self, request: ClinicSyncFetchRequest) -> ClinicSyncPage:
        if request.cursor is None:
            return ClinicSyncPage(
                domain=request.domain,
                cursor=None,
                next_cursor="patients:page:2",
                source_revision="rev-2",
                has_more=True,
                records=(ExternalPatientSyncRecord(external_id="P-1", first_name="One"),),
            )
        assert request.cursor == "patients:page:2"
        return ClinicSyncPage(
            domain=request.domain,
            cursor=request.cursor,
            next_cursor=None,
            source_revision="rev-2",
            records=(ExternalPatientSyncRecord(external_id="P-2", first_name="Two"),),
        )


class BrokenSource:
    sync_domains = frozenset({ClinicSyncDomain.PATIENTS})

    def fetch_sync_page(self, request: ClinicSyncFetchRequest) -> ClinicSyncPage:
        raise runtime.ClinicSyncRuntimeError("Provider temporarily unavailable.")


def test_prototype_connector_emits_deterministic_canonical_pages_and_allocations() -> None:
    adapter = PrototypeExternalClinicAdapter(
        workspace_timezone="Africa/Cairo",
        external_clinic_id="clinic-x",
        resolve_patient_external_id=lambda _value: None,
        config={
            "prototype_dataset": {
                "Clinic Timezone": "Africa/Cairo",
                "Source Revision": "rev-77",
                "Patients Sheet": [
                    {"Client Ref": "P-2", "Given Name": "B"},
                    {"Client Ref": "P-1", "Given Name": "A", "Mobile": "+20111"},
                ],
                "Payments Sheet": [
                    {
                        "Payment Ref": "R-1",
                        "Client Ref": "P-1",
                        "Transaction Kind": "payment",
                        "Amount": "1500",
                        "Currency": "EGP",
                        "Method": "cash",
                        "Created ISO": "2026-08-26T10:00:00+03:00",
                    }
                ],
                "Payment Allocations Sheet": [
                    {"Payment Ref": "R-1", "Booking Ref": "V-1", "Amount": "600"},
                    {"Payment Ref": "R-1", "Booking Ref": "V-1", "Amount": "400"},
                    {"Payment Ref": "R-1", "Booking Ref": "V-2", "Amount": "500"},
                ],
            }
        },
    )
    assert isinstance(adapter, ClinicSyncSource)
    first = adapter.fetch_sync_page(
        ClinicSyncFetchRequest(domain=ClinicSyncDomain.PATIENTS, limit=1)
    )
    assert first.records[0].external_id == "P-1"
    assert first.has_more is True
    assert first.next_cursor == "v1:patients:1"
    assert first.source_revision == "rev-77"

    second = adapter.fetch_sync_page(
        ClinicSyncFetchRequest(
            domain=ClinicSyncDomain.PATIENTS,
            cursor=first.next_cursor,
            limit=1,
        )
    )
    assert [record.external_id for record in second.records] == ["P-2"]
    assert second.has_more is False
    assert second.next_cursor is None

    payments = adapter.fetch_sync_page(
        ClinicSyncFetchRequest(domain=ClinicSyncDomain.PAYMENTS, limit=10)
    )
    payment = payments.records[0]
    assert payment.amount_minor == 150000
    assert [(item.appointment_external_id, item.amount_minor) for item in payment.allocations] == [
        ("V-1", 100000),
        ("V-2", 50000),
    ]


def test_scheduled_tick_claims_due_sync_advances_checkpoint_and_sets_next_run(
    db_workspace, monkeypatch
) -> None:
    db, workspace = db_workspace
    source = OnePatientSource()
    monkeypatch.setattr(runtime, "_source_for_workspace", lambda *_args, **_kwargs: source)

    schedule = runtime.update_sync_schedule(
        db,
        workspace=workspace,
        payload=ClinicSyncScheduleUpsert(enabled=True, interval_minutes=15),
        now=NOW,
    )
    db.commit()
    assert schedule.enabled is True
    assert schedule.next_run_at == NOW

    tick = runtime.run_scheduled_sync_tick(db, workspace=workspace, now=NOW)
    assert tick.claimed is True
    assert tick.cycle is not None and tick.cycle.status == "succeeded"
    assert tick.cycle.complete is True
    assert tick.cycle.domains[0].created_count == 1

    patient = db.scalar(select(Patient))
    assert patient is not None and patient.first_name == "Sara"
    checkpoint = db.get(
        ClinicIntegrationSyncCheckpoint,
        (workspace.id, ClinicSyncDomain.PATIENTS.value),
    )
    assert checkpoint is not None
    assert checkpoint.cursor is None
    assert checkpoint.source_revision == "rev-1"

    durable = db.get(ClinicIntegrationSyncSchedule, workspace.id)
    assert durable is not None
    assert durable.locked_at is None
    assert durable.attempts == 0
    assert runtime._utc(durable.next_run_at) == NOW + timedelta(minutes=15)
    assert durable.last_error is None

    not_due = runtime.run_scheduled_sync_tick(db, workspace=workspace, now=NOW)
    assert not_due.claimed is False
    assert not_due.reason == "not_due"
    assert source.calls == [None]


def test_page_budget_continues_from_checkpoint_on_next_tick(db_workspace, monkeypatch) -> None:
    db, workspace = db_workspace
    source = TwoPagePatientSource()
    monkeypatch.setattr(runtime, "_source_for_workspace", lambda *_args, **_kwargs: source)
    runtime.update_sync_schedule(
        db,
        workspace=workspace,
        payload=ClinicSyncScheduleUpsert(enabled=True, interval_minutes=15),
        now=NOW,
    )
    db.commit()

    first = runtime.run_scheduled_sync_tick(
        db,
        workspace=workspace,
        max_pages_per_domain=1,
        now=NOW,
    )
    assert first.claimed is True
    assert first.cycle is not None and first.cycle.complete is False
    checkpoint = db.get(
        ClinicIntegrationSyncCheckpoint,
        (workspace.id, ClinicSyncDomain.PATIENTS.value),
    )
    assert checkpoint is not None and checkpoint.cursor == "patients:page:2"
    schedule = db.get(ClinicIntegrationSyncSchedule, workspace.id)
    assert schedule is not None and runtime._utc(schedule.next_run_at) == NOW

    second = runtime.run_scheduled_sync_tick(
        db,
        workspace=workspace,
        max_pages_per_domain=1,
        now=NOW,
    )
    assert second.claimed is True
    assert second.cycle is not None and second.cycle.complete is True
    assert len(list(db.scalars(select(Patient)))) == 2


def test_connector_fetch_failure_is_observable_and_backed_off(db_workspace, monkeypatch) -> None:
    db, workspace = db_workspace
    monkeypatch.setattr(runtime, "_source_for_workspace", lambda *_args, **_kwargs: BrokenSource())
    runtime.update_sync_schedule(
        db,
        workspace=workspace,
        payload=ClinicSyncScheduleUpsert(enabled=True, interval_minutes=15),
        now=NOW,
    )
    db.commit()

    tick = runtime.run_scheduled_sync_tick(db, workspace=workspace, now=NOW)
    assert tick.claimed is True
    assert tick.cycle is not None and tick.cycle.status == "failed"
    failure = db.scalar(select(ClinicIntegrationSyncFailure))
    assert failure is not None
    assert failure.error_code == "connector_fetch_error"
    assert failure.message == "Provider temporarily unavailable."
    schedule = db.get(ClinicIntegrationSyncSchedule, workspace.id)
    assert schedule is not None
    assert schedule.attempts == 1
    assert runtime._utc(schedule.next_run_at) == NOW + timedelta(minutes=2)
    assert schedule.locked_at is None


def test_manual_sync_works_when_schedule_is_disabled(db_workspace, monkeypatch) -> None:
    db, workspace = db_workspace
    source = OnePatientSource()
    monkeypatch.setattr(runtime, "_source_for_workspace", lambda *_args, **_kwargs: source)

    cycle = runtime.run_manual_sync(db, workspace=workspace, domains=["patients"], now=NOW)
    assert cycle.status == "succeeded"
    schedule = db.get(ClinicIntegrationSyncSchedule, workspace.id)
    assert schedule is not None
    assert schedule.enabled is False
    assert schedule.next_run_at is None
    assert schedule.last_completed_at is not None



def test_fresh_schedule_lease_prevents_concurrent_tick(db_workspace, monkeypatch) -> None:
    db, workspace = db_workspace
    source = OnePatientSource()
    monkeypatch.setattr(runtime, "_source_for_workspace", lambda *_args, **_kwargs: source)
    runtime.update_sync_schedule(
        db,
        workspace=workspace,
        payload=ClinicSyncScheduleUpsert(enabled=True, interval_minutes=15),
        now=NOW,
    )
    schedule = db.get(ClinicIntegrationSyncSchedule, workspace.id)
    assert schedule is not None
    schedule.locked_at = NOW
    db.commit()

    tick = runtime.run_scheduled_sync_tick(db, workspace=workspace, now=NOW)
    assert tick.claimed is False
    assert tick.reason == "sync_locked"
    assert source.calls == []


def test_scheduled_misconfiguration_is_persisted_and_backed_off_not_raised(
    db_workspace, monkeypatch
) -> None:
    db, workspace = db_workspace
    source = OnePatientSource()
    monkeypatch.setattr(runtime, "_source_for_workspace", lambda *_args, **_kwargs: source)
    runtime.update_sync_schedule(
        db,
        workspace=workspace,
        payload=ClinicSyncScheduleUpsert(enabled=True, interval_minutes=15),
        now=NOW,
    )
    integration = db.get(runtime.ClinicIntegration, workspace.id)
    assert integration is not None
    integration.authority_policy_json = {
        "patients": {"owner": "external", "fields": {}},
        "payments": {"owner": "external", "fields": {}},
        "appointments": {"owner": "tia", "fields": {}},
    }
    db.commit()

    tick = runtime.run_scheduled_sync_tick(db, workspace=workspace, now=NOW)
    assert tick.claimed is False
    assert tick.reason is not None
    assert "payments" in tick.reason
    schedule = db.get(ClinicIntegrationSyncSchedule, workspace.id)
    assert schedule is not None
    assert schedule.locked_at is None
    assert schedule.attempts == 1
    assert runtime._utc(schedule.next_run_at) == NOW + timedelta(minutes=2)
    assert schedule.last_error == tick.reason
    assert source.calls == []


def test_manual_subset_does_not_require_unrequested_connector_domains(
    db_workspace, monkeypatch
) -> None:
    db, workspace = db_workspace
    source = OnePatientSource()
    monkeypatch.setattr(runtime, "_source_for_workspace", lambda *_args, **_kwargs: source)
    integration = db.get(runtime.ClinicIntegration, workspace.id)
    assert integration is not None
    integration.authority_policy_json = {
        "patients": {"owner": "external", "fields": {}},
        "payments": {"owner": "external", "fields": {}},
        "appointments": {"owner": "tia", "fields": {}},
    }
    db.commit()

    cycle = runtime.run_manual_sync(
        db,
        workspace=workspace,
        domains=["patients"],
        now=NOW,
    )
    assert cycle.status == "succeeded"
    assert [item.domain for item in cycle.domains] == ["patients"]
    assert source.calls == [None]
def test_phase63_migration_runtime_and_n8n_contracts_are_deterministic() -> None:
    backend = Path(__file__).resolve().parent.parent
    root = backend.parent
    migration = (backend / "alembic/versions/0036_sync_runtime.py").read_text(encoding="utf-8")
    readiness = (backend / "app/services/operational_readiness.py").read_text(encoding="utf-8")
    runtime_source = (backend / "app/services/clinic_integration_sync_runtime.py").read_text(
        encoding="utf-8"
    ).lower()
    clinic_routes = (backend / "app/api/routes/clinic.py").read_text(encoding="utf-8")
    automation_routes = (backend / "app/api/routes/automations.py").read_text(encoding="utf-8")
    workflow = json.loads((root / "n8n/workflows/tia_automation_scheduler.json").read_text(encoding="utf-8"))

    assert 'revision: str = "0036_sync_runtime"' in migration
    assert len("0036_sync_runtime") <= 32
    assert "clinic_integration_sync_schedules" in migration
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness
    assert '"/integration/sync/run"' in clinic_routes
    assert '"/integration/sync/schedule"' in clinic_routes
    assert '"/adapter/clinic-sync/tick"' in automation_routes
    assert any(node.get("name") == "Linka Clinic Sync Tick" for node in workflow["nodes"])
    assert "eval(" not in runtime_source
    assert "re.compile" not in runtime_source
    assert "re.search" not in runtime_source
    assert "keyword" not in runtime_source