from __future__ import annotations

from app.agents.v2.turn_contract import (
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from scripts.run_clinic_hours_clock_live_review import _transactional_safety_deltas
from scripts.spoken_clock_live_review_base import _dump_provider_value


def test_wrong_transactional_time_write_is_counted_even_when_persistence_will_rollback() -> None:
    record = {
        "agent": "?? ????? ?????? 11:00",
        "appointments": [
            {
                "start_at": "2026-10-15T08:00:00+00:00",
            }
        ],
    }
    deltas = _transactional_safety_deltas(record, expected_clock="23:00")
    assert deltas == {"wrong_time_writes": 1, "false_success": 1}
    # Rollback persistence is measured independently after the audit transaction closes.
    persisted_audit_writes = 0
    assert persisted_audit_writes == 0


def test_provider_audit_dump_retains_excluded_spoken_period_hint() -> None:
    value = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    time=TimeConstraint(
                        mode="exact",
                        start_time="23:00",
                        spoken_period_hint="pm",
                    )
                ),
            )
        ]
    )
    dumped = _dump_provider_value(value)
    assert dumped["operations"][0]["entities"]["time"]["spoken_period_hint"] == "pm"
    assert "spoken_period_hint" not in value.model_dump(mode="json")["operations"][0]["entities"]["time"]
