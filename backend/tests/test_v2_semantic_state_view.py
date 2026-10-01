from __future__ import annotations

import json

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import (
    with_safe_action_context,
    with_safe_task_context,
)

SERVICE_ID = "123e4567-e89b-12d3-a456-426614174001"
DOCTOR_ID = "123e4567-e89b-12d3-a456-426614174002"
APPOINTMENT_ID = "123e4567-e89b-12d3-a456-426614174003"
PACKAGE_ID = "123e4567-e89b-12d3-a456-426614174004"


def _context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": SERVICE_ID,
                    "name": "ليزر إبط",
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela"}
                    ],
                }
            ],
            "doctors": [
                {"id": DOCTOR_ID, "name": "د. مريم", "service_ids": [SERVICE_ID]}
            ],
            "appointments": [
                {
                    "appointment_id": APPOINTMENT_ID,
                    "service_id": SERVICE_ID,
                    "doctor_id": DOCTOR_ID,
                    "status": "confirmed",
                    "start_local": "2026-09-17T19:00:00+03:00",
                }
            ],
            "packages": [
                {
                    "id": PACKAGE_ID,
                    "name": "باكيدج إبط",
                    "service_id": SERVICE_ID,
                    "laser_device_key": "candela_gentle",
                    "remaining_sessions": 4,
                }
            ],
        }
    )


def test_booking_task_view_replaces_canonical_ids_with_ephemeral_refs() -> None:
    context = _context()
    active = {
        "task_type": "booking",
        "status": "awaiting_choice",
        "write_authorization": {
            "operation": "booking",
            "authorized": True,
            "source_turn_id": "internal-turn-123",
        },
        "constraints": {
            "service_id": SERVICE_ID,
            "doctor_id": DOCTOR_ID,
            "device_key": "candela_gentle",
            "date": {"mode": "exact", "start_date": "2026-09-17", "end_date": None},
            "time": {"mode": "after", "start_time": "18:00", "end_time": None},
            "package_usage": "use_existing",
        },
        "derived": {
            "selected_package_id": PACKAGE_ID,
            "availability_snapshot_id": "internal-snapshot-1",
        },
        "option_snapshot": {
            "snapshot_id": "internal-snapshot-2",
            "purpose": "booking_slot",
            "options": [
                {
                    "ref": "internal-slot-ref",
                    "label": "7 مساءً",
                    "payload": {
                        "doctor_id": DOCTOR_ID,
                        "service_id": SERVICE_ID,
                        "device_key": "candela_gentle",
                        "laser_device_name": "Candela",
                        "start_time_24h": "19:00",
                        "doctor_name": "د. مريم",
                    },
                }
            ],
        },
    }

    safe = with_safe_task_context(context, active_task=active)
    payload = json.dumps(safe.model_input, ensure_ascii=False, default=str)

    assert SERVICE_ID not in payload
    assert DOCTOR_ID not in payload
    assert PACKAGE_ID not in payload
    assert "internal-turn-123" not in payload
    assert "internal-snapshot" not in payload
    assert "internal-slot-ref" not in payload
    assert '"service_ref": "S1"' in payload
    assert '"doctor_ref": "D1"' in payload
    assert '"device_ref": "V1"' in payload
    assert '"start_time_24h": "19:00"' in payload
    assert '"device_ref": "V1"' in payload
    assert '"laser_device_name": "Candela"' in payload


def test_reschedule_task_view_uses_appointment_ref_without_payment_or_write_metadata() -> None:
    context = _context()
    active = {
        "task_type": "reschedule",
        "status": "collecting",
        "write_authorization": {
            "operation": "reschedule",
            "authorized": True,
            "source_turn_id": "secret-turn",
        },
        "target": {
            "appointment_id": APPOINTMENT_ID,
            "service_id": SERVICE_ID,
            "doctor_id": DOCTOR_ID,
            "start_local": "2026-09-17T19:00:00+03:00",
            "payment_context": {"secret": "internal"},
        },
        "replacement": {
            "service_id": SERVICE_ID,
            "date": {"mode": "exact", "start_date": "2026-09-19", "end_date": None},
            "package_usage": "unspecified",
        },
    }

    safe = with_safe_task_context(context, active_task=active)
    payload = json.dumps(safe.model_input, ensure_ascii=False, default=str)

    assert APPOINTMENT_ID not in payload
    assert SERVICE_ID not in payload
    assert "payment_context" not in payload
    assert "write_authorization" not in payload
    assert "secret-turn" not in payload
    assert '"appointment_ref": "A1"' in payload



def test_verified_pulse_purchase_action_view_uses_ephemeral_device_ref() -> None:
    context = _context()
    safe = with_safe_action_context(
        context,
        action_context={
            "operation_type": "buy_pulse_pack",
            "device_key": "candela_gentle",
            "pulse_count": 1000,
            "patient_pulse_pack_id": "internal-pack-id",
            "amount_paid_minor": 0,
        },
    )

    recent = safe.model_input["recent_verified_action"]
    assert recent == {
        "operation_type": "buy_pulse_pack",
        "device_ref": "V1",
        "pulse_count": 1000,
    }
    encoded = json.dumps(safe.model_input, ensure_ascii=False, default=str)
    assert "candela_gentle" not in encoded
    assert "internal-pack-id" not in encoded
    assert "amount_paid_minor" not in encoded

def test_legacy_pulse_usage_is_not_exposed_to_the_model() -> None:
    context = _context()
    active = {
        "task_type": "booking",
        "status": "collecting",
        "constraints": {
            "service_id": SERVICE_ID,
            "device_key": "candela_gentle",
            "package_usage": "unspecified",
            "pulse_usage": "use_existing",
        },
    }

    safe = with_safe_task_context(context, active_task=active)
    payload = json.dumps(safe.model_input, ensure_ascii=False, default=str)

    assert "pulse_usage" not in payload
    assert "use_existing" not in payload


def test_verified_booking_action_view_uses_only_ephemeral_refs() -> None:
    context = _context()
    safe = with_safe_action_context(
        context,
        action_context={
            "operation_type": "book",
            "appointment_id": APPOINTMENT_ID,
            "service_id": SERVICE_ID,
            "doctor_id": DOCTOR_ID,
            "device_key": "candela_gentle",
            "start_at": "2026-09-17T16:00:00+00:00",
            "status": "confirmed",
            "date": {"mode": "exact", "start_date": "2026-09-17"},
            "time": {"mode": "exact", "start_time": "19:00"},
            "package_usage": "unspecified",
        },
    )

    recent = safe.model_input["recent_verified_action"]
    assert recent["operation_type"] == "book"
    assert recent["appointment_ref"] == "A1"
    assert recent["service_ref"] == "S1"
    assert recent["doctor_ref"] == "D1"
    assert recent["device_ref"] == "V1"
    assert recent["status"] == "confirmed"
    encoded = json.dumps(recent, ensure_ascii=False, default=str)
    assert SERVICE_ID not in encoded
    assert DOCTOR_ID not in encoded
    assert APPOINTMENT_ID not in encoded
    assert "candela_gentle" not in encoded


def test_verified_cancellation_action_view_hides_canonical_appointment_id() -> None:
    context = _context()
    safe = with_safe_action_context(
        context,
        action_context={
            "operation_type": "cancel_appointment",
            "appointment_id": APPOINTMENT_ID,
            "status": "cancelled",
        },
    )

    recent = safe.model_input["recent_verified_action"]
    assert recent == {
        "operation_type": "cancel_appointment",
        "status": "cancelled",
        "appointment_ref": "A1",
    }
    assert APPOINTMENT_ID not in json.dumps(recent, ensure_ascii=False)


def test_verified_booking_action_creates_ephemeral_appointment_ref_without_catalog_appointments() -> None:
    context = build_semantic_context(
        {
            "services": [
                {
                    "id": SERVICE_ID,
                    "name": "هيدرافيشل",
                    "requires_laser_device": False,
                }
            ],
            "doctors": [
                {
                    "id": DOCTOR_ID,
                    "name": "مريم",
                    "service_ids": [SERVICE_ID],
                }
            ],
            "appointments": [],
            "packages": [],
        }
    )
    safe = with_safe_action_context(
        context,
        action_context={
            "operation_type": "book",
            "appointment_id": APPOINTMENT_ID,
            "service_id": SERVICE_ID,
            "doctor_id": DOCTOR_ID,
            "start_at": "2026-09-17T16:00:00+00:00",
            "status": "confirmed",
            "package_usage": "unspecified",
        },
    )

    recent = safe.model_input["recent_verified_action"]
    appointment_ref = recent["appointment_ref"]
    assert isinstance(appointment_ref, str)
    assert safe.resolve(appointment_ref, expected_kind="appointment") == APPOINTMENT_ID

    encoded = json.dumps(safe.model_input, ensure_ascii=False, default=str)
    assert APPOINTMENT_ID not in encoded
    assert SERVICE_ID not in encoded
    assert DOCTOR_ID not in encoded


def test_verified_booking_action_reuses_existing_appointment_ref() -> None:
    context = _context()
    safe = with_safe_action_context(
        context,
        action_context={
            "operation_type": "book",
            "appointment_id": APPOINTMENT_ID,
            "service_id": SERVICE_ID,
            "doctor_id": DOCTOR_ID,
            "start_at": "2026-09-17T16:00:00+00:00",
            "status": "confirmed",
            "package_usage": "unspecified",
        },
    )

    assert safe.model_input["recent_verified_action"]["appointment_ref"] == "A1"
    appointment_targets = [
        target
        for target in safe.reference_map.values()
        if target.kind == "appointment"
    ]
    assert len(appointment_targets) == 1
