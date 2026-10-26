from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes import crm as crm_routes
from app.integrations.clinic.authority import ClinicIntegrationAuthorityError
from app.models.patient import Patient
from app.schemas.crm import PatientUpdate


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Patient.__table__.create(engine)
    return engine


def _patient(db: Session, workspace_id, **kwargs) -> Patient:
    patient = Patient(
        workspace_id=workspace_id,
        first_name=kwargs.pop("first_name", "Mona"),
        last_name=kwargs.pop("last_name", "Ali"),
        phone=kwargs.pop("phone", "01012345678"),
        phone_normalized=kwargs.pop("phone_normalized", "01012345678"),
        customer_id=kwargs.pop("customer_id", "A-1045"),
        source=kwargs.pop("source", "walk_in"),
        **kwargs,
    )
    db.add(patient)
    db.commit()
    return patient


def _access(workspace_id):
    return SimpleNamespace(workspace=SimpleNamespace(id=workspace_id), user=SimpleNamespace(id=uuid4()))


@pytest.fixture(autouse=True)
def _default_writable(monkeypatch):
    monkeypatch.setattr(crm_routes, "require_tia_patient_fields_writable", lambda *args, **kwargs: None)


def test_p1_n3_edit_legacy_name_stores_one_field(monkeypatch) -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        result = crm_routes.update_patient(
            patient.id,
            PatientUpdate(first_name="Mona Ali Hassan", last_name=None),
            _access(workspace_id),
            db,
        )
        assert result.first_name == "Mona Ali Hassan"
        assert result.last_name is None


def test_p2_edit_phone_is_normalized() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        result = crm_routes.update_patient(
            patient.id,
            PatientUpdate(phone="010 5555 6677"),
            _access(workspace_id),
            db,
        )
        assert result.phone == "010 5555 6677"
        assert result.phone_normalized
        assert "01055556677" in result.phone_normalized.replace("+20", "0") or result.phone_normalized.endswith("1055556677")


def test_p3_edit_id_succeeds() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        result = crm_routes.update_patient(
            patient.id,
            PatientUpdate(customer_id="B-2200"),
            _access(workspace_id),
            db,
        )
        assert result.customer_id == "B-2200"


def test_p4_edit_source_succeeds() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        result = crm_routes.update_patient(
            patient.id,
            PatientUpdate(source="instagram"),
            _access(workspace_id),
            db,
        )
        assert result.source == "instagram"


def test_p5_edit_all_four_fields_commits_together() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        result = crm_routes.update_patient(
            patient.id,
            PatientUpdate(
                first_name="Mona Ali Hassan",
                last_name=None,
                phone="01112345678",
                customer_id="B-2200",
                source="referral",
            ),
            _access(workspace_id),
            db,
        )
        assert (result.first_name, result.last_name, result.phone, result.customer_id, result.source) == (
            "Mona Ali Hassan",
            None,
            "01112345678",
            "B-2200",
            "referral",
        )


def test_p6_duplicate_workspace_phone_blocks_all_partial_updates() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id, phone="01011111111", phone_normalized="01011111111")
        other = _patient(
            db,
            workspace_id,
            first_name="Other",
            last_name=None,
            phone="01122222222",
            phone_normalized="01122222222",
            customer_id="OTHER",
        )
        patient_id = patient.id
        with pytest.raises(HTTPException) as error:
            crm_routes.update_patient(
                patient_id,
                PatientUpdate(
                    first_name="Changed Name",
                    last_name=None,
                    phone=other.phone,
                    customer_id="CHANGED",
                    source="instagram",
                ),
                _access(workspace_id),
                db,
            )
        assert error.value.status_code == 409
        assert error.value.detail == crm_routes.DUPLICATE_PATIENT_MESSAGE
        restored = db.scalar(select(Patient).where(Patient.id == patient_id))
        assert (restored.first_name, restored.last_name, restored.customer_id, restored.source) == (
            "Mona",
            "Ali",
            "A-1045",
            "walk_in",
        )
        assert restored.phone == "01011111111"


def test_p7_same_phone_in_another_workspace_is_not_a_collision() -> None:
    engine = _engine()
    workspace_a, workspace_b = uuid4(), uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_a, phone="01011111111", phone_normalized="01011111111")
        _patient(
            db,
            workspace_b,
            first_name="Foreign",
            last_name=None,
            phone="01122222222",
            phone_normalized="01122222222",
        )
        result = crm_routes.update_patient(
            patient.id,
            PatientUpdate(phone="01122222222"),
            _access(workspace_a),
            db,
        )
        assert result.phone == "01122222222"


def test_p8_foreign_workspace_patient_is_not_found() -> None:
    engine = _engine()
    workspace_a, workspace_b = uuid4(), uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_a)
        with pytest.raises(HTTPException) as error:
            crm_routes.update_patient(
                patient.id,
                PatientUpdate(customer_id="LEAK"),
                _access(workspace_b),
                db,
            )
        assert error.value.status_code == 404
        assert db.get(Patient, patient.id).customer_id == "A-1045"


def test_p9_changed_externally_owned_field_is_blocked(monkeypatch) -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)

        def block_phone(*args, fields, **kwargs):
            assert fields == {"phone"}
            raise ClinicIntegrationAuthorityError("phone is externally owned")

        monkeypatch.setattr(crm_routes, "require_tia_patient_fields_writable", block_phone)
        with pytest.raises(HTTPException) as error:
            crm_routes.update_patient(
                patient.id,
                PatientUpdate(phone="01112345678"),
                _access(workspace_id),
                db,
            )
        assert error.value.status_code == 409
        assert db.get(Patient, patient.id).phone == "01012345678"


def test_p10_unchanged_external_field_is_not_sent_or_checked(monkeypatch) -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id)
        seen: list[set[str]] = []

        def assert_only_changed(*args, fields, **kwargs):
            seen.append(fields)
            if "phone" in fields:
                raise ClinicIntegrationAuthorityError("phone is externally owned")

        monkeypatch.setattr(crm_routes, "require_tia_patient_fields_writable", assert_only_changed)
        result = crm_routes.update_patient(
            patient.id,
            PatientUpdate(customer_id="NEW-ID"),
            _access(workspace_id),
            db,
        )
        assert result.customer_id == "NEW-ID"
        assert result.phone == "01012345678"
        assert seen == [{"customer_id"}]


def test_n2_search_matches_legacy_full_display_name() -> None:
    engine = _engine()
    workspace_id = uuid4()
    with Session(engine) as db:
        patient = _patient(db, workspace_id, first_name="Mona", last_name="Ali")
        results = crm_routes.list_patients(
            _access(workspace_id),
            db,
            q="Mona Ali",
            patient_status=None,
            source=None,
            limit=50,
            offset=0,
        )
        assert [row.id for row in results] == [patient.id]
