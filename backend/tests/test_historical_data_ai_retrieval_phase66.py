from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.agents.tools import clinic_tools
from app.agents.tools.clinic_tools import AgentToolContext, build_clinic_tools
from app.integrations.clinic.mapped_sync import canonicalize_raw_sync_page
from app.integrations.clinic.sync_contract import ClinicRawSyncPage, ClinicSyncDomain
from app.models.patient import Patient
from app.schemas.clinic_connector_mapping import ClinicSyncMapping, PatientSyncMapping
from app.services.agent_v2.planner import ReadRequest
from app.services.agent_v2.read_executor import (
    ReadExecutionContext,
    _read_customer_history,
    _read_customer_profile,
)
from app.services.patient_history import build_patient_history_context, historical_analytics

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
OLD = datetime(2022, 1, 10, 9, 0, tzinfo=UTC)


def _schema(engine) -> None:
    ddl = [
        """
        CREATE TABLE patients (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            first_name VARCHAR(120), last_name VARCHAR(120), phone VARCHAR(40),
            phone_normalized VARCHAR(40), customer_id TEXT, gender VARCHAR(32), birth_date DATE,
            preferred_language VARCHAR(10), preferred_branch_id CHAR(32), source VARCHAR(32),
            source_detail VARCHAR(200), status VARCHAR(20), marketing_consent BOOLEAN DEFAULT 0,
            marketing_consent_at DATETIME, whatsapp_opt_in BOOLEAN DEFAULT 0,
            whatsapp_opt_in_at DATETIME, whatsapp_opt_in_source VARCHAR(32), source_created_at DATETIME, last_contact_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE services (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL, name VARCHAR(180) NOT NULL
        )
        """,
        """
        CREATE TABLE branches (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL, name VARCHAR(180) NOT NULL
        )
        """,
        """
        CREATE TABLE staff (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            first_name VARCHAR(120), last_name VARCHAR(120)
        )
        """,
        """
        CREATE TABLE doctors (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL, staff_id CHAR(32) NOT NULL
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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        """
        CREATE TABLE payment_allocations (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), transaction_id CHAR(32),
            appointment_id CHAR(32), amount_minor INTEGER, created_at DATETIME
        )
        """,
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.exec_driver_sql(statement)


def _seed(db: Session):
    workspace_id = uuid4()
    patient_id = uuid4()
    service_id = uuid4()
    branch_id = uuid4()
    doctor_id = uuid4()
    staff_id = uuid4()
    appointment_1 = uuid4()
    appointment_2 = uuid4()
    payment_1 = uuid4()
    payment_2 = uuid4()
    rows = [
        ("INSERT INTO patients (id,workspace_id,first_name,last_name,phone,phone_normalized,gender,birth_date,preferred_language,source,status,marketing_consent,source_created_at,created_at,updated_at) VALUES (:id,:w,'Mona','Ali','0100','0100','female','1990-04-03','ar','other','active',0,:source_created,:created,:created)", {"id": patient_id.hex, "w": workspace_id.hex, "source_created": OLD.isoformat(), "created": NOW.isoformat()}),
        ("INSERT INTO services (id,workspace_id,name) VALUES (:id,:w,'Laser')", {"id": service_id.hex, "w": workspace_id.hex}),
        ("INSERT INTO branches (id,workspace_id,name) VALUES (:id,:w,'New Cairo')", {"id": branch_id.hex, "w": workspace_id.hex}),
        ("INSERT INTO staff (id,workspace_id,first_name,last_name) VALUES (:id,:w,'Sara','Hassan')", {"id": staff_id.hex, "w": workspace_id.hex}),
        ("INSERT INTO doctors (id,workspace_id,staff_id) VALUES (:id,:w,:staff)", {"id": doctor_id.hex, "w": workspace_id.hex, "staff": staff_id.hex}),
    ]
    for appointment_id, start, price in [
        (appointment_1, datetime(2022, 2, 1, 10, 0, tzinfo=UTC), 100000),
        (appointment_2, datetime(2022, 3, 1, 10, 0, tzinfo=UTC), 120000),
    ]:
        end = start.replace(hour=11)
        rows.append(("INSERT INTO appointments (id,workspace_id,patient_id,branch_id,doctor_id,service_id,status,source,start_at,end_at,busy_start_at,busy_end_at,duration_minutes,price_minor,currency,payment_status,amount_paid_minor,payment_method,created_at,updated_at) VALUES (:id,:w,:p,:b,:d,:s,'completed','other',:start,:end,:start,:end,60,:price,'EGP','paid',:price,'cash',:start,:start)", {"id": appointment_id.hex,"w":workspace_id.hex,"p":patient_id.hex,"b":branch_id.hex,"d":doctor_id.hex,"s":service_id.hex,"start":start.isoformat(),"end":end.isoformat(),"price":price}))
    rows.extend([
        ("INSERT INTO payment_transactions (id,workspace_id,appointment_id,origin_appointment_id,patient_id,transaction_type,amount_minor,currency,payment_method,source,created_at) VALUES (:id,:w,:a,:a,:p,'payment',100000,'EGP','cash','integration',:at)", {"id":payment_1.hex,"w":workspace_id.hex,"a":appointment_1.hex,"p":patient_id.hex,"at":datetime(2022,2,1,10,30,tzinfo=UTC).isoformat()}),
        ("INSERT INTO payment_transactions (id,workspace_id,appointment_id,origin_appointment_id,patient_id,transaction_type,amount_minor,currency,payment_method,source,created_at) VALUES (:id,:w,:a,:a,:p,'payment',120000,'EGP','cash','integration',:at)", {"id":payment_2.hex,"w":workspace_id.hex,"a":appointment_2.hex,"p":patient_id.hex,"at":datetime(2022,3,1,10,30,tzinfo=UTC).isoformat()}),
        ("INSERT INTO payment_allocations (id,workspace_id,transaction_id,appointment_id,amount_minor,created_at) VALUES (:id,:w,:t,:a,100000,:at)", {"id":uuid4().hex,"w":workspace_id.hex,"t":payment_1.hex,"a":appointment_1.hex,"at":datetime(2022,2,1,10,30,tzinfo=UTC).isoformat()}),
        ("INSERT INTO payment_allocations (id,workspace_id,transaction_id,appointment_id,amount_minor,created_at) VALUES (:id,:w,:t,:a,120000,:at)", {"id":uuid4().hex,"w":workspace_id.hex,"t":payment_2.hex,"a":appointment_2.hex,"at":datetime(2022,3,1,10,30,tzinfo=UTC).isoformat()}),
    ])
    for sql, params in rows:
        db.execute(text(sql), params)
    db.commit()
    return workspace_id, patient_id


def test_patient_mapping_hydrates_demographics_and_original_created_at() -> None:
    mapping = ClinicSyncMapping(
        patients=PatientSyncMapping(
            sheet="clients",
            external_id="client_id",
            first_name="name",
            gender="gender",
            birth_date="dob",
            source_created_at="created_at",
        )
    )
    page = canonicalize_raw_sync_page(
        raw_page=ClinicRawSyncPage(
            domain=ClinicSyncDomain.PATIENTS,
            tables={"clients": ({"client_id":"C1","name":"Mona","gender":"female","dob":"1990-04-03","created_at":"2022-01-10T09:00:00+00:00"},)},
            schema_fingerprint="fp",
        ),
        mapping=mapping,
        expected_schema_fingerprint="fp",
    )
    record = page.records[0]
    assert record.gender == "female"
    assert record.birth_date == date(1990, 4, 3)
    assert record.source_created_at == OLD


def test_patient_history_context_exposes_old_visits_services_and_payments() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _schema(engine)
    with Session(engine) as db:
        workspace_id, patient_id = _seed(db)
        patient = db.get(Patient, patient_id)
        context = build_patient_history_context(db, workspace_id=workspace_id, patient=patient)
        assert context.profile.effective_first_seen_at.year == 2022
        assert context.profile.tia_created_at.year == 2026
        assert context.completed_appointments == 2
        assert context.services[0].service_name == "Laser"
        assert context.services[0].completed_visits == 2
        assert context.money[0].net_paid_minor == 220000
        assert len(context.recent_appointments) == 2
        assert context.recent_appointments[0].net_paid_minor == 120000


def test_historical_analytics_uses_full_history_and_repeat_patients() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _schema(engine)
    with Session(engine) as db:
        workspace_id, _patient_id = _seed(db)
        result = historical_analytics(db, workspace_id=workspace_id)
        assert result.data_start_at.year == 2022
        assert result.total_patients == 1
        assert result.repeat_patients == 1
        assert result.repeat_patient_rate_percent == 100.0
        assert result.completed_appointments == 2
        assert result.money[0].net_paid_minor == 220000
        assert result.top_services[0].unique_patients == 1


def test_customer_history_is_read_only_semantic_capability() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = (root / "app/agents/capability_policy.py").read_text(encoding="utf-8")
    router = (root / "app/agents/turn_models.py").read_text(encoding="utf-8")
    customer_agent = (root / "app/agents/tia_customer_agent.py").read_text(encoding="utf-8")
    assert '"customer_history": frozenset({"get_customer_history"})' in policy
    assert '"customer_history"' in router
    assert '"get_customer_history"' in customer_agent
    assert '"get_customer_history": "' not in (root / "app/agents/capability_policy.py").read_text(encoding="utf-8").split("WRITE_TOOL_CAPABILITY", 1)[1].split("}", 1)[0]


def test_history_routes_and_analytics_use_canonical_data_without_runtime_ai() -> None:
    root = Path(__file__).resolve().parents[1]
    crm = (root / "app/api/routes/crm.py").read_text(encoding="utf-8")
    analytics_route = (root / "app/api/routes/analytics.py").read_text(encoding="utf-8")
    analytics = (root / "app/services/analytics.py").read_text(encoding="utf-8")
    history = (root / "app/services/patient_history.py").read_text(encoding="utf-8").lower()
    assert '"/patients/{patient_id}/history-context"' in crm
    assert '@router.get("/history"' in analytics_route
    assert "func.coalesce(Patient.source_created_at, Patient.created_at)" in analytics
    assert "invoke_model" not in history and "langchain" not in history


def test_migration_preserves_original_patient_timestamp_and_head() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = (root / "alembic/versions/0037_patient_history.py").read_text(encoding="utf-8")
    readiness = (root / "app/services/operational_readiness.py").read_text(encoding="utf-8")
    assert 'revision: str = "0037_patient_history"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0036_sync_runtime"' in migration
    assert 'sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True)' in migration
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness
    assert len("0037_patient_history") <= 32

def test_populated_customer_id_is_absent_from_agent_tool_and_agent_v2_payloads(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _schema(engine)
    customer_id_secret = "PRIVATE-CUSTOMER-ID-009812"
    monkeypatch.setattr(clinic_tools, "_record_action", lambda *args, **kwargs: None)

    with Session(engine) as db:
        workspace_id, patient_id = _seed(db)
        patient = db.get(Patient, patient_id)
        patient.customer_id = customer_id_secret
        db.commit()

        history = build_patient_history_context(db, workspace_id=workspace_id, patient=patient)
        history_json = history.model_dump_json()
        assert customer_id_secret not in history_json
        assert "customer_id" not in history_json

        agent_context = AgentToolContext(
            db=db,
            workspace=SimpleNamespace(id=workspace_id),
            patient=patient,
            conversation=SimpleNamespace(id=uuid4()),
            run_id=uuid4(),
        )
        tools = {item.name: item for item in build_clinic_tools(agent_context)}
        for tool_name in ("get_customer_profile", "get_customer_history"):
            tool_payload = tools[tool_name].invoke({})
            assert customer_id_secret not in tool_payload
            assert "customer_id" not in tool_payload

        v2_context = ReadExecutionContext(
            db=db,
            workspace=SimpleNamespace(id=workspace_id),
            patient=patient,
            now=NOW,
        )
        for result in (
            _read_customer_profile(ReadRequest(kind="customer_profile"), v2_context),
            _read_customer_history(ReadRequest(kind="customer_history"), v2_context),
        ):
            payload_json = json.dumps(result.model_dump(mode="json"))
            assert customer_id_secret not in payload_json
            assert "customer_id" not in payload_json
