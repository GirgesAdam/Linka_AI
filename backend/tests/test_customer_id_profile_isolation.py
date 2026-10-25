from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes import crm as crm_routes
from app.database.session import get_db
from app.models.patient import Patient
from app.schemas.crm import (
    PatientCreate,
    PatientCRMStats,
    PatientProfilePatientRead,
    PatientProfileRead,
    PatientRead,
    PatientUpdate,
)
from app.schemas.patient_history import PatientHistoryContextRead, PatientHistoryProfileRead


def test_customer_id_is_optional_text_and_preserves_leading_zeroes() -> None:
    without_id = PatientCreate(first_name="Existing")
    with_id = PatientCreate(first_name="Clinic managed", customer_id="001204")

    assert without_id.customer_id is None
    assert with_id.customer_id == "001204"
    assert PatientUpdate(customer_id="001204").customer_id == "001204"
    assert PatientUpdate(customer_id=None).customer_id is None
    assert "customer_id" in Patient.__table__.columns
    assert Patient.__table__.columns["customer_id"].nullable is True


def test_customer_id_is_profile_only_not_general_or_agent_facing_schema() -> None:
    assert "customer_id" in PatientProfilePatientRead.model_fields
    assert "customer_id" not in PatientRead.model_fields
    assert "customer_id" not in PatientHistoryProfileRead.model_fields
    assert "customer_id" not in PatientHistoryContextRead.model_fields


def test_customer_id_is_exposed_only_by_profile_route_and_profile_ui() -> None:
    backend = Path(__file__).resolve().parents[1]
    routes = (backend / "app/api/routes/crm.py").read_text(encoding="utf-8")
    schemas = (backend / "app/schemas/crm.py").read_text(encoding="utf-8")
    profile_builder = (backend / "app/services/patient_timeline.py").read_text(encoding="utf-8")
    assert '@router.get("/patients", response_model=list[PatientRead])' in routes
    assert '@router.get("/patients/{patient_id}", response_model=PatientRead)' in routes
    assert '@router.get("/patients/{patient_id}/profile", response_model=PatientProfileRead)' in routes
    assert 'patient: PatientProfilePatientRead' in schemas
    assert 'customer_id: str | None = None' in schemas
    assert 'patient=PatientProfilePatientRead.model_validate(patient)' in profile_builder


def _profile_for_test(patient):
    return PatientProfileRead(
        patient=PatientProfilePatientRead.model_validate(patient),
        stats=PatientCRMStats(
            total_appointments=0,
            completed_appointments=0,
            no_show_appointments=0,
            upcoming_appointments=0,
            total_conversations=0,
            open_conversations=0,
            active_handoffs=0,
            active_leads=0,
            next_appointment_at=None,
            last_appointment_at=None,
        ),
        tags=[],
        notes=[],
        timeline=[],
        latest_conversation_id=None,
    )


def test_customer_id_create_update_clear_and_profile_retrieval(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Patient.__table__.create(engine)
    workspace_id = uuid4()
    access = SimpleNamespace(
        workspace=SimpleNamespace(id=workspace_id),
        user=SimpleNamespace(id=uuid4()),
    )
    monkeypatch.setattr(crm_routes, "require_tia_patient_fields_writable", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        crm_routes,
        "build_patient_profile",
        lambda db, workspace_id, patient, timeline_limit: _profile_for_test(patient),
    )

    with Session(engine) as db:
        without_id = crm_routes.create_patient(PatientCreate(first_name="Without ID"), access, db)
        assert without_id.customer_id is None

        patient = crm_routes.create_patient(
            PatientCreate(first_name="Clinic customer", customer_id="001204"), access, db
        )
        assert patient.customer_id == "001204"
        assert crm_routes.get_patient_profile(patient.id, access, db).patient.customer_id == "001204"

        # General detail/list responses deliberately use PatientRead, not the profile DTO.
        assert "customer_id" not in PatientRead.model_validate(crm_routes.get_patient(patient.id, access, db)).model_dump()
        assert all("customer_id" not in PatientRead.model_validate(row).model_dump() for row in crm_routes.list_patients(access, db))

        updated = crm_routes.update_patient(
            patient.id, PatientUpdate(customer_id="CLINIC-002300"), access, db
        )
        assert updated.customer_id == "CLINIC-002300"
        assert crm_routes.get_patient_profile(patient.id, access, db).patient.customer_id == "CLINIC-002300"

        cleared = crm_routes.update_patient(patient.id, PatientUpdate(customer_id=None), access, db)
        assert cleared.customer_id is None
        assert crm_routes.get_patient_profile(patient.id, access, db).patient.customer_id is None


def test_customer_id_cannot_be_read_or_modified_across_workspaces(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Patient.__table__.create(engine)
    workspace_id = uuid4()
    access = SimpleNamespace(workspace=SimpleNamespace(id=workspace_id), user=SimpleNamespace(id=uuid4()))
    other_access = SimpleNamespace(workspace=SimpleNamespace(id=uuid4()), user=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(crm_routes, "require_tia_patient_fields_writable", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        crm_routes,
        "build_patient_profile",
        lambda db, workspace_id, patient, timeline_limit: _profile_for_test(patient),
    )

    with Session(engine) as db:
        patient = crm_routes.create_patient(
            PatientCreate(first_name="Tenant private", customer_id="PRIVATE-001"), access, db
        )
        with pytest.raises(HTTPException) as read_error:
            crm_routes.get_patient_profile(patient.id, other_access, db)
        assert read_error.value.status_code == 404

        with pytest.raises(HTTPException) as update_error:
            crm_routes.update_patient(patient.id, PatientUpdate(customer_id="TAMPERED"), other_access, db)
        assert update_error.value.status_code == 404
        assert db.get(Patient, patient.id).customer_id == "PRIVATE-001"


def test_customer_id_profile_read_and_write_require_existing_authentication() -> None:
    app = FastAPI()
    app.include_router(crm_routes.router)
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)
    patient_id = uuid4()

    read_response = client.get(f"/patients/{patient_id}/profile")
    update_response = client.patch(f"/patients/{patient_id}", json={"customer_id": "UNAUTHORIZED"})

    assert read_response.status_code == 401
    assert update_response.status_code == 401
