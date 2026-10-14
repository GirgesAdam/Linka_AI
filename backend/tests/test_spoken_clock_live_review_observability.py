from __future__ import annotations

from types import SimpleNamespace

from scripts.run_clinic_hours_clock_live_review import (
    SAFETY_COUNTERS,
    ScenarioSpec,
    _observe_transactional_safety,
)


def _reset() -> None:
    for key in SAFETY_COUNTERS:
        SAFETY_COUNTERS[key] = 0


def test_wrong_transactional_time_write_is_counted_independently_from_rollback() -> None:
    _reset()
    spec = ScenarioSpec(
        name="reviewer-regression",
        customer="11 بالليل",
        start="09:00",
        end="23:30",
        expected_canonical="23:00",
        expected_hint="pm",
    )
    ctx = SimpleNamespace(bikini=SimpleNamespace(id="svc-bikini"))
    record = {
        "agent": "تم الحجز الساعة 11 صباحًا",
        "appointments": [
            {
                "start_at": "2026-10-15T08:00:00+00:00",
                "service_id": "svc-bikini",
                "laser_device_key": "candela_gentle",
                "additional_services": [],
            }
        ],
        "proposals_created": [],
        "db_effect": {"appointment_delta": 1},
    }

    _observe_transactional_safety(record, spec, ctx)

    assert SAFETY_COUNTERS["wrong_time_writes"] == 1
    assert SAFETY_COUNTERS["false_success"] == 1
    assert SAFETY_COUNTERS["persisted_audit_writes"] == 0
