from dataclasses import fields
from inspect import getsource

from app.agents.tools import clinic_tools
from app.integrations.clinic.base import RescheduleAppointmentRequest
from app.integrations.clinic.tia_database import TiaDatabaseClinicAdapter
from app.services import agent_chat, appointment_operations


def test_reschedule_contract_carries_laser_device_key() -> None:
    assert "laser_device_key" in {
        field.name for field in fields(RescheduleAppointmentRequest)
    }


def test_agent_prefetch_forwards_grounded_device_to_booking_and_reschedule() -> None:
    source = getsource(agent_chat._prefetch_read_tools)

    assert 'laser_device_key = text_value("laser_device_key")' in source
    assert '"laser_device_key": laser_device_key' in source
    assert 'reschedule_arguments["laser_device_key"] = laser_device_key' in source


def test_clinic_tools_forward_device_to_verified_availability_and_writes() -> None:
    source = getsource(clinic_tools)

    assert "laser_device_key=laser_device_key" in source
    assert "target_device_key = laser_device_key or current.laser_device_key" in source
    assert "laser_device_key=target_device_key" in source
    assert "laser_device_key=current_laser_device_key()" in source
    assert '"laser_device_key": getattr(slot, "laser_device_key", None)' in source


def test_native_adapter_validates_package_against_exact_slot_device() -> None:
    source = getsource(TiaDatabaseClinicAdapter.create_appointment)

    assert "laser_device_key=request.laser_device_key" in source
    assert "laser_device_key=slot.laser_device_key" in source
    assert "laser_device_name=slot.laser_device_name" in source


def test_paid_or_package_sensitive_device_change_uses_existing_handoff_boundary() -> None:
    source = getsource(appointment_operations.reschedule_appointment_operation)

    assert "device_changed = new_laser_device_key != current.laser_device_key" in source
    assert "if service_changed or device_changed:" in source
    assert "service_change_requires_human(" in source
    assert "laser_device_key=new_laser_device_key" in source
