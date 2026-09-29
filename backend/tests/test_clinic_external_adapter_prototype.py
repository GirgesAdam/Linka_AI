from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.integrations.clinic.base import (
    AppointmentReadRequest,
    AvailabilityRequest,
    CancelAppointmentRequest,
    ClinicCapability,
    ClinicCapabilityNotSupported,
)
from app.integrations.clinic.prototype_external import (
    PrototypeExternalClinicAdapter,
    PrototypeExternalConfigurationError,
)
from app.integrations.clinic.registry import get_clinic_adapter, registered_clinic_adapter_keys
from app.models.clinic_integration import ClinicIntegration
from app.schemas.clinic_integration import ClinicIntegrationUpsert


def _prototype_dataset() -> dict:
    return {
        "Clinic Timezone": "Africa/Cairo",
        "Source Revision": "sheet-export-2026-08-22T18:00:00+03:00",
        "Status Map": {"OK": "confirmed", "WAIT": "pending"},
        "Treatments Sheet": [
            {
                "Treatment Code": "TR-LSR-U",
                "Treatment": "ليزر إزالة الشعر - إبط",
                "Category": "Laser",
                "Description": "External sheet row",
                "Minutes": "30",
                "Price": "550",
                "Currency": "EGP",
                "Medical Review Required": False,
            },
            {
                "Treatment Code": "TR-LSR-F",
                "Treatment": "ليزر إزالة الشعر",
                "Category": "Laser",
                "Minutes": "45",
                "Price": "1500.00",
                "Currency": "EGP",
                "Medical Review Required": False,
            },
        ],
        "Locations Sheet": [
            {
                "Branch Ref": "LOC-NASR",
                "Branch Code": "NASR",
                "Clinic Location": "فرع مدينة نصر",
                "City": "Cairo",
                "Address": "شارع تجريبي 10",
            },
            {
                "Branch Ref": "LOC-ZAYED",
                "Clinic Location": "فرع الشيخ زايد",
                "City": "Giza",
            },
        ],
        "Doctors Sheet": [
            {
                "Doctor Key": "EMP-AHM-17",
                "Doctor Display": "د. أحمد محمود",
                "Specialty": "Dermatology",
                "Treatments": "TR-LSR-U|TR-LSR-F",
                "Locations": "LOC-NASR",
            },
            {
                "Doctor Key": "EMP-OMR-2",
                "Doctor Display": "د. عمر علي",
                "Specialty": "Aesthetics",
                "Treatments": ["TR-LSR-F"],
                "Locations": ["LOC-ZAYED"],
            },
        ],
        "Free Slots Feed": [
            {
                "Branch Ref": "LOC-NASR",
                "Treatment Code": "TR-LSR-U",
                "Doctor Key": "EMP-AHM-17",
                "Start ISO": "2026-08-25T18:15:00+03:00",
            },
            {
                "Branch Ref": "LOC-NASR",
                "Treatment Code": "TR-LSR-U",
                "Doctor Key": "EMP-AHM-17",
                "Start ISO": "2026-08-25T20:15:00+03:00",
            },
            {
                "Branch Ref": "LOC-ZAYED",
                "Treatment Code": "TR-LSR-F",
                "Doctor Key": "EMP-OMR-2",
                "Start ISO": "2026-08-25T19:00:00+03:00",
            },
        ],
        "Bookings Sheet": [
            {
                "Booking Ref": "BOOK-EXT-443",
                "Client Ref": "CLIENT-009",
                "Treatment Code": "TR-LSR-U",
                "Branch Ref": "LOC-NASR",
                "Doctor Key": "EMP-AHM-17",
                "Start ISO": "2026-08-25T20:15:00+03:00",
                "Status": "OK",
            },
            {
                "Booking Ref": "BOOK-OTHER-1",
                "Client Ref": "CLIENT-OTHER",
                "Treatment Code": "TR-LSR-F",
                "Branch Ref": "LOC-ZAYED",
                "Doctor Key": "EMP-OMR-2",
                "Start ISO": "2026-08-25T19:00:00+03:00",
                "Status": "WAIT",
            },
        ],
    }


def _adapter(*, resolver=lambda _patient_id: "CLIENT-009") -> PrototypeExternalClinicAdapter:
    return PrototypeExternalClinicAdapter(
        workspace_timezone="Africa/Cairo",
        external_clinic_id="SHEET-CLINIC-77",
        config={"prototype_dataset": _prototype_dataset()},
        resolve_patient_external_id=resolver,
    )


def test_external_prototype_normalizes_messy_catalog_without_tia_orm() -> None:
    adapter = _adapter()
    catalog = adapter.build_catalog()

    assert catalog["services"][0]["id"] in {"TR-LSR-F", "TR-LSR-U"}
    service = next(row for row in catalog["services"] if row["id"] == "TR-LSR-U")
    assert service["name"] == "ليزر إزالة الشعر - إبط"
    assert service["duration_minutes"] == 30
    assert service["price_minor"] == 55000

    branch = next(row for row in catalog["branches"] if row["id"] == "LOC-NASR")
    assert branch["name"] == "فرع مدينة نصر"
    assert branch["address"] == "شارع تجريبي 10، Cairo"

    doctor = next(row for row in catalog["doctors"] if row["id"] == "EMP-AHM-17")
    assert doctor["service_ids"] == ["TR-LSR-F", "TR-LSR-U"]
    assert doctor["branch_ids"] == ["LOC-NASR"]
    assert adapter.catalog_revision() == "sheet-export-2026-08-22T18:00:00+03:00"

    source = (
        Path(__file__).resolve().parent.parent
        / "app/integrations/clinic/prototype_external.py"
    )
    text = source.read_text(encoding="utf-8")
    assert "app.models.appointment" not in text
    assert "app.models.service" not in text
    assert "sqlalchemy" not in text.lower()


def test_external_prototype_returns_canonical_availability_from_slot_feed() -> None:
    adapter = _adapter()
    result = adapter.get_availability(
        AvailabilityRequest(
            branch_id="LOC-NASR",
            service_id="TR-LSR-U",
            doctor_id="EMP-AHM-17",
            booking_date=date(2026, 8, 25),
            now=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        )
    )

    assert result.timezone == "Africa/Cairo"
    assert result.branch_name == "فرع مدينة نصر"
    assert result.service_price_minor == 55000
    assert [slot.doctor_id for slot in result.slots] == ["EMP-AHM-17", "EMP-AHM-17"]
    assert [slot.start_at.hour for slot in result.slots] == [18, 20]
    assert all(slot.duration_minutes == 30 for slot in result.slots)


def test_external_prototype_uses_patient_entity_link_for_appointment_reads() -> None:
    seen: list[str] = []

    def resolve(canonical_patient_id: str) -> str | None:
        seen.append(canonical_patient_id)
        return "CLIENT-009"

    adapter = _adapter(resolver=resolve)
    result = adapter.get_patient_appointments(
        AppointmentReadRequest(
            patient_id="TIA-PATIENT-UUID-1",
            now=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        )
    )

    assert seen == ["TIA-PATIENT-UUID-1"]
    assert len(result.appointments) == 1
    appointment = result.appointments[0]
    assert appointment.appointment_id == "BOOK-EXT-443"
    assert appointment.patient_id == "TIA-PATIENT-UUID-1"
    assert appointment.status == "confirmed"  # raw external "OK" is normalized
    assert appointment.service_id == "TR-LSR-U"
    assert appointment.doctor_name == "د. أحمد محمود"
    assert appointment.price_minor == 55000


def test_external_prototype_fails_if_tia_patient_has_no_external_link() -> None:
    adapter = _adapter(resolver=lambda _patient_id: None)
    with pytest.raises(ValueError, match="not linked"):
        adapter.get_patient_appointments(
            AppointmentReadRequest(patient_id="TIA-PATIENT-UUID-1")
        )


def test_external_prototype_is_read_only_and_advertises_capabilities() -> None:
    adapter = _adapter()
    assert adapter.capabilities.supports(ClinicCapability.CATALOG_READ)
    assert adapter.capabilities.supports(ClinicCapability.AVAILABILITY_READ)
    assert adapter.capabilities.supports(ClinicCapability.APPOINTMENTS_READ)
    assert not adapter.capabilities.supports(ClinicCapability.APPOINTMENTS_CANCEL)

    with pytest.raises(ClinicCapabilityNotSupported):
        adapter.cancel_appointment(
            CancelAppointmentRequest(
                patient_id="TIA-PATIENT-UUID-1",
                appointment_id="BOOK-EXT-443",
                operation_id="RUN-1",
            )
        )


def test_registry_can_resolve_active_external_prototype() -> None:
    workspace = SimpleNamespace(id=uuid4(), timezone="Africa/Cairo")
    integration = SimpleNamespace(
        workspace_id=workspace.id,
        mode="external_api",
        adapter_key="prototype_external",
        status="active",
        external_clinic_id="SHEET-CLINIC-77",
        secret_ref=None,
        config_json={"prototype_dataset": _prototype_dataset()},
    )

    class _Db:
        @staticmethod
        def get(model, identity):
            assert model is ClinicIntegration
            assert identity == workspace.id
            return integration

        @staticmethod
        def scalar(_statement):
            # Simulates clinic_integration_entity_links resolving the Linka patient.
            return "CLIENT-009"

    adapter = get_clinic_adapter(db=_Db(), workspace=workspace)
    assert isinstance(adapter, PrototypeExternalClinicAdapter)
    assert adapter.build_catalog()["services"]
    appointments = adapter.get_patient_appointments(
        AppointmentReadRequest(
            patient_id="TIA-PATIENT-UUID-1",
            now=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        )
    )
    assert appointments.appointments[0].appointment_id == "BOOK-EXT-443"
    assert "prototype_external" in registered_clinic_adapter_keys()


def test_active_prototype_config_requires_dataset_and_external_api_mode() -> None:
    with pytest.raises(ValidationError, match="prototype_dataset"):
        ClinicIntegrationUpsert(
            mode="external_api",
            adapter_key="prototype_external",
            status="active",
            config={},
        )

    with pytest.raises(ValidationError, match="external_api prototype"):
        ClinicIntegrationUpsert(
            mode="hybrid",
            adapter_key="prototype_external",
            status="active",
            config={"prototype_dataset": _prototype_dataset()},
        )

    payload = ClinicIntegrationUpsert(
        mode="external_api",
        adapter_key="prototype_external",
        status="active",
        external_clinic_id="SHEET-CLINIC-77",
        config={"prototype_dataset": _prototype_dataset()},
    )
    assert payload.adapter_key == "prototype_external"


def test_unknown_external_status_fails_instead_of_guessing() -> None:
    dataset = _prototype_dataset()
    dataset["Bookings Sheet"][0]["Status"] = "MYSTERY"
    adapter = PrototypeExternalClinicAdapter(
        workspace_timezone="Africa/Cairo",
        external_clinic_id="SHEET-CLINIC-77",
        config={"prototype_dataset": dataset},
        resolve_patient_external_id=lambda _patient_id: "CLIENT-009",
    )
    with pytest.raises(PrototypeExternalConfigurationError, match="no canonical mapping"):
        adapter.get_patient_appointments(
            AppointmentReadRequest(
                patient_id="TIA-PATIENT-UUID-1",
                now=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
            )
        )


def test_shipped_prototype_example_is_valid_active_integration_payload() -> None:
    example = (
        Path(__file__).resolve().parent.parent
        / "examples/clinic_integrations/prototype_external_integration.json"
    )
    payload = ClinicIntegrationUpsert.model_validate_json(example.read_text(encoding="utf-8"))
    assert payload.mode == "external_api"
    assert payload.adapter_key == "prototype_external"
    assert payload.status == "active"
