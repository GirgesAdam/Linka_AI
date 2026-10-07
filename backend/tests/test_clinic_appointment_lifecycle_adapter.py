from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.integrations.clinic.base import (
    CancelAppointmentRequest,
    ClinicAdapter,
    ClinicCapabilities,
    ClinicCapability,
    ClinicCapabilityNotSupported,
    ConfirmAppointmentRequest,
    CreateAppointmentRequest,
    RescheduleAppointmentRequest,
)


class _ReadOnlyAdapter(ClinicAdapter):
    @property
    def capabilities(self) -> ClinicCapabilities:
        return ClinicCapabilities(frozenset({ClinicCapability.CATALOG_READ}))

    def build_catalog(self):
        return {"services": [], "branches": [], "doctors": []}

    def get_availability(self, request):
        raise NotImplementedError

    def get_patient_appointments(self, request):
        raise NotImplementedError


def test_unsupported_write_capability_is_blocked_at_adapter_boundary() -> None:
    adapter = _ReadOnlyAdapter()
    start = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)

    with pytest.raises(ClinicCapabilityNotSupported):
        adapter.create_appointment(
            CreateAppointmentRequest(
                patient_id="PAT-1",
                branch_id="BR-1",
                service_id="SVC-1",
                doctor_id="DR-1",
                start_at=start,
                operation_id="RUN-1",
            )
        )

    with pytest.raises(ClinicCapabilityNotSupported):
        adapter.cancel_appointment(
            CancelAppointmentRequest(
                patient_id="PAT-1",
                appointment_id="BOOKING-99",
                operation_id="RUN-1",
            )
        )


def test_lifecycle_request_ids_are_source_agnostic_strings() -> None:
    start = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)
    create = CreateAppointmentRequest(
        patient_id="EXCEL-PATIENT-7",
        branch_id="BR-TAGAMO3",
        service_id="LASER-FACE",
        doctor_id="DR-SARA-17",
        start_at=start,
        operation_id="wa-msg-991",
    )
    confirm = ConfirmAppointmentRequest(
        patient_id=create.patient_id,
        appointment_id="BOOKING-443",
        operation_id=create.operation_id,
    )
    cancel = CancelAppointmentRequest(
        patient_id=create.patient_id,
        appointment_id=confirm.appointment_id,
        operation_id=create.operation_id,
        reason="customer_requested",
    )
    reschedule = RescheduleAppointmentRequest(
        patient_id=create.patient_id,
        appointment_id=confirm.appointment_id,
        start_at=start + timedelta(days=1),
        operation_id=create.operation_id,
        branch_id="BR-ZAYED",
        doctor_id="DR-OMAR-2",
    )

    assert create.patient_id == "EXCEL-PATIENT-7"
    assert create.doctor_id == "DR-SARA-17"
    assert confirm.appointment_id == "BOOKING-443"
    assert cancel.appointment_id == "BOOKING-443"
    assert reschedule.branch_id == "BR-ZAYED"
    assert reschedule.doctor_id == "DR-OMAR-2"


def test_agent_lifecycle_tools_delegate_writes_to_adapter() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")

    start = source.index("    def book_appointment(")
    end = source.index("\n    @tool\n    def escalate_to_human(", start)
    lifecycle_source = source[start:end]

    assert "adapter.create_appointment(" in lifecycle_source
    assert "CreateAppointmentRequest(" in lifecycle_source
    assert "adapter.confirm_appointment(" in lifecycle_source
    assert "ConfirmAppointmentRequest(" in lifecycle_source
    assert "adapter.cancel_appointment(" in lifecycle_source
    assert "CancelAppointmentRequest(" in lifecycle_source
    assert "adapter.reschedule_appointment(" in lifecycle_source
    assert "RescheduleAppointmentRequest(" in lifecycle_source

    assert "Appointment(" not in lifecycle_source
    assert "select(Appointment)" not in lifecycle_source
    assert "find_exact_slot(" not in lifecycle_source
    assert "AppointmentStatusHistory(" not in lifecycle_source


def test_reschedule_discovery_reads_appointments_through_adapter() -> None:
    backend = Path(__file__).resolve().parent.parent
    source = (backend / "app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")
    start = source.index("    def get_reschedule_options(")
    end = source.index("\n    @tool\n    def get_available_slots(", start)
    function_source = source[start:end]

    assert "_adapter_patient_appointments(" in function_source
    assert "_canonical_appointment_summary(" in function_source
    assert "select(Appointment)" not in function_source
    assert "current.branch_id" in function_source
    assert "current.service_id" in function_source
    assert "exclude_appointment_id=current.appointment_id" in function_source


def test_native_adapter_owns_lifecycle_policy_and_native_history() -> None:
    backend = Path(__file__).resolve().parent.parent
    tool_source = (backend / "app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")
    adapter_source = (
        backend / "app/integrations/clinic/tia_database.py"
    ).read_text(encoding="utf-8")

    assert "from app.models.appointment import Appointment" not in tool_source
    assert (
        "from app.models.appointment_status_history import AppointmentStatusHistory"
        not in tool_source
    )
    assert "find_exact_slot" not in tool_source
    assert "get_effective_booking_settings" not in tool_source

    assert "def create_appointment(" in adapter_source
    assert "def confirm_appointment(" in adapter_source
    assert "def cancel_appointment(" in adapter_source
    assert "def reschedule_appointment(" in adapter_source
    assert "find_exact_slot(" in adapter_source
    assert "initial_confirmation_decision(" in adapter_source
    assert "AppointmentStatusHistory(" in adapter_source
    assert "Lead.status.notin_" in adapter_source
    assert "ClinicActionRequiresHuman(" in adapter_source
