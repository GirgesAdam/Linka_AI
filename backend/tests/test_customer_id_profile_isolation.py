from pathlib import Path

from app.models.patient import Patient
from app.schemas.crm import (
    PatientCreate,
    PatientProfilePatientRead,
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
