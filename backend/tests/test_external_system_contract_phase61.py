from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.integrations.clinic.base import (
    ClinicCapability,
    PatientReadRequest,
    PaymentReadRequest,
)
from app.integrations.clinic.prototype_external import (
    PrototypeExternalClinicAdapter,
    PrototypeExternalConfigurationError,
)
from app.integrations.clinic.tia_database import TiaDatabaseClinicAdapter
from app.models.patient import Patient
from app.schemas.clinic_integration import ClinicEntityLinkUpsert


def _external_dataset() -> dict:
    return {
        "Clinic Timezone": "Africa/Cairo",
        "Patients Sheet": [
            {
                "Client Ref": "CLIENT-009",
                "Given Name": "Sara",
                "Family Name": "Ali",
                "Mobile": "+201000000009",
                "State": "ENABLED",
                "Language": "ar",
                "Acquisition": "whatsapp",
                "Updated ISO": "2026-08-25T19:00:00+03:00",
            }
        ],
        "Payments Sheet": [
            {
                "Payment Ref": "PAY-EXT-100",
                "Client Ref": "CLIENT-009",
                "Booking Ref": "BOOK-EXT-443",
                "Transaction Kind": "CHARGE",
                "Amount": "500.00",
                "Currency": "EGP",
                "Method": "Credit Card",
                "Created ISO": "2026-08-25T19:05:00+03:00",
                "Provider Ref": "POS-9001",
            },
            {
                "Payment Ref": "REF-EXT-101",
                "Client Ref": "CLIENT-009",
                "Booking Ref": "BOOK-EXT-443",
                "Transaction Kind": "REVERSAL",
                "Amount": "100",
                "Currency": "EGP",
                "Method": "Credit Card",
                "Created ISO": "2026-08-25T20:05:00+03:00",
                "Original Payment Ref": "PAY-EXT-100",
            },
            {
                "Payment Ref": "PAY-OTHER",
                "Client Ref": "CLIENT-OTHER",
                "Booking Ref": "BOOK-OTHER",
                "Transaction Kind": "PAYMENT",
                "Amount": "1",
                "Currency": "EGP",
                "Method": "Cash",
                "Created ISO": "2026-08-25T20:10:00+03:00",
            },
        ],
    }


def _external_adapter(dataset: dict | None = None) -> PrototypeExternalClinicAdapter:
    return PrototypeExternalClinicAdapter(
        workspace_timezone="Africa/Cairo",
        external_clinic_id="VENDOR-77",
        config={"prototype_dataset": dataset or _external_dataset()},
        resolve_patient_external_id=lambda patient_id: (
            "CLIENT-009" if patient_id == "TIA-PATIENT-1" else None
        ),
    )


def test_external_contract_advertises_patient_and_payment_reads_only_when_present() -> None:
    adapter = _external_adapter()
    assert adapter.capabilities.supports(ClinicCapability.PATIENTS_READ)
    assert adapter.capabilities.supports(ClinicCapability.PAYMENTS_READ)

    no_crm = _external_dataset()
    no_crm.pop("Patients Sheet")
    no_crm.pop("Payments Sheet")
    adapter_without_sections = _external_adapter(no_crm)
    assert not adapter_without_sections.capabilities.supports(ClinicCapability.PATIENTS_READ)
    assert not adapter_without_sections.capabilities.supports(ClinicCapability.PAYMENTS_READ)


def test_external_patient_schema_is_normalized_to_canonical_record() -> None:
    patient = _external_adapter().get_patient(PatientReadRequest(patient_id="TIA-PATIENT-1"))
    assert patient.patient_id == "TIA-PATIENT-1"
    assert patient.first_name == "Sara"
    assert patient.last_name == "Ali"
    assert patient.status == "active"
    assert patient.preferred_language == "ar"
    assert patient.source == "whatsapp"
    assert patient.updated_at is not None
    assert patient.updated_at.utcoffset() is not None


def test_external_payment_schema_normalizes_payment_and_refund_facts() -> None:
    result = _external_adapter().get_patient_payments(
        PaymentReadRequest(patient_id="TIA-PATIENT-1", appointment_id="BOOK-EXT-443")
    )
    assert [row.transaction_id for row in result.transactions] == ["REF-EXT-101", "PAY-EXT-100"]
    refund, payment = result.transactions
    assert payment.transaction_type == "payment"
    assert payment.amount_minor == 50000
    assert payment.payment_method == "card"
    assert payment.source == "integration"
    assert payment.external_reference == "POS-9001"
    assert refund.transaction_type == "refund"
    assert refund.amount_minor == 10000
    assert refund.reference_transaction_id == "PAY-EXT-100"


def test_external_refund_without_original_payment_fails_closed() -> None:
    dataset = _external_dataset()
    dataset["Payments Sheet"][1].pop("Original Payment Ref")
    with pytest.raises(PrototypeExternalConfigurationError, match="original payment"):
        _external_adapter(dataset).get_patient_payments(
            PaymentReadRequest(patient_id="TIA-PATIENT-1")
        )


def test_native_adapter_exposes_patient_and_payment_read_capabilities() -> None:
    adapter = TiaDatabaseClinicAdapter(
        db=object(),
        workspace=SimpleNamespace(id=uuid4(), timezone="Africa/Cairo"),
    )
    assert adapter.capabilities.supports(ClinicCapability.PATIENTS_READ)
    assert adapter.capabilities.supports(ClinicCapability.PAYMENTS_READ)


def test_native_patient_read_stays_behind_adapter_boundary() -> None:
    workspace_id = uuid4()
    patient_id = uuid4()
    row = SimpleNamespace(
        id=patient_id,
        workspace_id=workspace_id,
        first_name="Mona",
        last_name=None,
        phone=None,
        status="active",
        preferred_language="ar",
        source="phone",
        updated_at=datetime(2026, 8, 25, 20, 0, tzinfo=UTC),
    )

    class Db:
        @staticmethod
        def get(model, identity):
            assert model is Patient
            assert identity == patient_id
            return row

    record = TiaDatabaseClinicAdapter(
        db=Db(), workspace=SimpleNamespace(id=workspace_id, timezone="Africa/Cairo")
    ).get_patient(PatientReadRequest(patient_id=str(patient_id)))
    assert record.patient_id == str(patient_id)
    assert record.first_name == "Mona"
    assert record.source == "phone"


def test_payment_is_valid_external_entity_link_type() -> None:
    payload = ClinicEntityLinkUpsert(
        entity_type="payment",
        canonical_id=str(uuid4()),
        external_id="PAY-VENDOR-1",
    )
    assert payload.entity_type == "payment"


def test_phase61_migration_expands_entity_link_constraint_without_new_sync_table() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "alembic/versions/0030_external_system_contract.py").read_text(
        encoding="utf-8"
    )
    assert "0029_payment_ledger" in source
    assert "'payment'" in source
    assert "clinic_integration_entity_links" in source
    assert "create_table" not in source


def test_external_prototype_still_has_no_tia_orm_or_sqlalchemy_dependency() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/integrations/clinic/prototype_external.py").read_text(
        encoding="utf-8"
    ).lower()
    assert "app.models." not in source
    assert "sqlalchemy" not in source


def test_admin_runtime_diagnostics_expose_capabilities_and_link_counts() -> None:
    backend = Path(__file__).resolve().parent.parent
    route_source = (backend / "app/api/routes/clinic.py").read_text(encoding="utf-8")
    service_source = (backend / "app/services/clinic_integration_runtime.py").read_text(encoding="utf-8")
    assert '@router.get("/integration/runtime"' in route_source
    assert 'require_page_permission_workspace("setup", legacy_access="admin")' in route_source
    assert "build_clinic_integration_runtime" in route_source
    assert "adapter.capabilities.as_dict()" in service_source
    assert "func.count(ClinicIntegrationEntityLink.id)" in service_source


def test_clinic_settings_surfaces_historical_import_contract() -> None:
    root = Path(__file__).resolve().parent.parent.parent
    setup = (root / "frontend/src/app/(dashboard)/setup/page.tsx").read_text(encoding="utf-8")
    panel = (
        root / "frontend/src/app/(dashboard)/setup/clinic-settings-panel.tsx"
    ).read_text(encoding="utf-8")
    legacy_page = (
        root / "frontend/src/app/(dashboard)/setup/integration/page.tsx"
    ).read_text(encoding="utf-8")
    source = setup + panel

    assert "HistoricalImportUploader" in panel
    assert "HistoricalBatch" in source
    assert '"/clinic/history/batches"' in setup
    assert 'href="/api/clinic-history-template"' in panel
    assert 'redirect("/setup#historical-data")' in legacy_page


def test_shipped_external_example_contains_patient_and_payment_vendor_sections() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (
        backend / "examples/clinic_integrations/prototype_external_integration.json"
    ).read_text(encoding="utf-8")
    assert '"Patients Sheet"' in source
    assert '"Payments Sheet"' in source
    assert '"Transaction Kind": "CHARGE"' in source
    assert '"Transaction Kind": "REVERSAL"' in source


def test_runtime_diagnostics_service_returns_adapter_capabilities(monkeypatch) -> None:
    from app.integrations.clinic.base import ClinicCapabilities
    from app.models.clinic_integration import ClinicIntegration
    from app.models.clinic_integration_sync import ClinicIntegrationSyncSchedule
    from app.services import clinic_integration_runtime as runtime_service

    workspace_id = uuid4()
    workspace = SimpleNamespace(id=workspace_id, timezone="Africa/Cairo")
    integration = SimpleNamespace(
        workspace_id=workspace_id,
        mode="external_api",
        adapter_key="prototype_external",
        status="active",
    )

    class Result:
        @staticmethod
        def all():
            return [("patient", 2), ("payment", 3)]

    class Db:
        @staticmethod
        def get(model, identity):
            assert identity == workspace_id
            if model is ClinicIntegration:
                return integration
            if model is ClinicIntegrationSyncSchedule:
                return None
            raise AssertionError(f"Unexpected model lookup: {model}")

        @staticmethod
        def execute(_statement):
            return Result()

    fake_adapter = SimpleNamespace(
        capabilities=ClinicCapabilities(
            frozenset({ClinicCapability.PATIENTS_READ, ClinicCapability.PAYMENTS_READ})
        ),
        cache_namespace="vendor:clinic-77",
    )
    monkeypatch.setattr(
        runtime_service,
        "registered_clinic_adapter_keys",
        lambda: frozenset({"prototype_external"}),
    )
    monkeypatch.setattr(runtime_service, "get_clinic_adapter", lambda **_: fake_adapter)

    result = runtime_service.build_clinic_integration_runtime(Db(), workspace)
    assert result.adapter_installed is True
    assert result.adapter_active is True
    assert result.cache_namespace == "vendor:clinic-77"
    assert result.capabilities["patients.read"] is True
    assert result.capabilities["payments.read"] is True
    assert result.entity_link_counts == {"patient": 2, "payment": 3}
