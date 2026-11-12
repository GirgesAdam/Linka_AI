"""Visiting Doctor retirement: explicit compatibility failures, no implicit conversion."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from openpyxl import Workbook, load_workbook
from pydantic import ValidationError

from app.api.routes.clinic_setup_v2 import save_visiting_doctor_windows_v2
from app.schemas.clinic import DoctorCreate, DoctorUpdate
from app.schemas.clinic_setup_v2 import (
    ClinicDoctorCreateV2,
    ClinicDoctorUpdateV2,
    ClinicSetupDraft,
    VisitingWindowsUpdateV2,
)
from app.services.clinic_setup_import import (
    ClinicSetupImportError,
    build_clinic_setup_template,
    clinic_setup_draft_to_workbook_base64,
    preview_clinic_setup_workbook,
)
from app.services.clinic_setup_v2 import ClinicSetupV2Error, replace_visiting_windows_v2


def test_new_doctor_has_single_default_type_without_selection() -> None:
    doctor = ClinicDoctorCreateV2(full_name="Synthetic Doctor")
    assert doctor.doctor_type == "regular"
    assert ClinicDoctorUpdateV2(full_name="Synthetic Doctor").doctor_type is None


@pytest.mark.parametrize("payload_model,fields", [
    (ClinicDoctorCreateV2, {"full_name": "Synthetic Doctor", "doctor_type": "visiting"}),
    (ClinicDoctorUpdateV2, {"full_name": "Synthetic Doctor", "doctor_type": "visiting"}),
    (DoctorCreate, {"staff_id": str(uuid4()), "doctor_type": "visiting"}),
    (DoctorUpdate, {"doctor_type": "visiting"}),
])
def test_visiting_doctor_type_is_explicitly_rejected_by_legacy_api_contracts(payload_model, fields):
    with pytest.raises(ValidationError):
        payload_model.model_validate(fields)


def test_legacy_visiting_endpoint_returns_410_without_touching_data() -> None:
    with pytest.raises(HTTPException) as exc:
        save_visiting_doctor_windows_v2(
            doctor_id=uuid4(),
            payload=VisitingWindowsUpdateV2(),
            access=None, db=None,
        )
    assert exc.value.status_code == 410
    with pytest.raises(ClinicSetupV2Error, match="retired"):
        replace_visiting_windows_v2(
            None, workspace=None, doctor_id=uuid4(),
            payload=VisitingWindowsUpdateV2(),
        )


def test_new_template_has_weekly_hours_but_no_visiting_window_sheet_or_type() -> None:
    workbook = load_workbook(io.BytesIO(build_clinic_setup_template()), read_only=True)
    try:
        assert "doctor_hours" in workbook.sheetnames
        assert "visiting_windows" not in workbook.sheetnames
        assert [cell.value for cell in next(workbook["doctors"].iter_rows())] == [
            "full_name", "specialization",
        ]
    finally:
        workbook.close()


def _preview(workbook: Workbook):
    stream = io.BytesIO()
    workbook.save(stream)
    return preview_clinic_setup_workbook(
        filename="synthetic.xlsx",
        content_base64=base64.b64encode(stream.getvalue()).decode("ascii"),
    )


def test_legacy_visiting_sheet_is_rejected_even_before_any_apply() -> None:
    workbook = Workbook()
    workbook.active.title = "doctors"
    workbook.active.append(["full_name"])
    workbook.active.append(["Synthetic Doctor"])
    old_sheet = workbook.create_sheet("visiting_windows")
    old_sheet.append(["doctor_name", "date", "start_time", "end_time"])
    old_sheet.append(["Synthetic Doctor", "2026-11-20", "10:00", "12:00"])
    try:
        with pytest.raises(ClinicSetupImportError, match="retired"):
            _preview(workbook)
    finally:
        workbook.close()


def test_legacy_visiting_value_in_doctor_sheet_is_rejected() -> None:
    workbook = Workbook()
    workbook.active.title = "doctors"
    workbook.active.append(["full_name", "doctor_type"])
    workbook.active.append(["Synthetic Doctor", "visiting"])
    try:
        with pytest.raises(ClinicSetupImportError, match="retired"):
            _preview(workbook)
    finally:
        workbook.close()


def test_apply_draft_cannot_silently_discard_legacy_visiting_window_rows() -> None:
    draft = ClinicSetupDraft(visiting_windows=[{
        "doctor_name": "Synthetic Doctor", "date": "2026-11-20",
        "start_time": "10:00", "end_time": "12:00",
    }])
    with pytest.raises(ClinicSetupImportError, match="retired"):
        clinic_setup_draft_to_workbook_base64(draft)


def test_archival_import_retains_passive_doctors_without_weekly_time_synthesis() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/services/historical_import.py").read_text(encoding="utf-8")
    section = source.split("def _create_archival_doctor(", 1)[1].split(
        "def _unassigned_historical_doctor(", 1
    )[0]
    assert 'doctor_type="regular"' in section
    assert "booking_enabled=False" in section
    assert "DoctorWorkingHour(" not in section


def test_weekly_bookability_never_uses_legacy_window_or_type() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "app/services/booking.py").read_text(encoding="utf-8")
    assert "DoctorWorkingHour" in service
    assert "DoctorAvailabilityWindow" not in service
    assert "doctor.doctor_type" not in service
