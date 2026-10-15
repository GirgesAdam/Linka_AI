from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_clinic_hours_clock_live_review import (
    _transactional_safety_evidence,
    _transactional_wrong_time_writes,
)


def test_wrong_transactional_time_is_counted_even_when_persistence_later_rolls_back() -> None:
    record = {
        "appointments_before": [],
        "appointments": [
            {"id": "a1", "start_at": "2026-10-15T08:00:00+00:00"}
        ],
        "db_effect": {"appointment_delta": 1},
    }
    record["agent"] = "?? ?????"
    assert _transactional_safety_evidence(record, "23:00") == {
        "wrong_time_writes": 1,
        "false_success": 1,
    }
    persisted_audit_writes_after_rollback = 0
    assert persisted_audit_writes_after_rollback == 0


def test_correct_transactional_time_is_not_counted_as_wrong_time() -> None:
    record = {
        "appointments_before": [],
        "appointments": [
            {"id": "a1", "start_at": "2026-10-15T20:00:00+00:00"}
        ],
    }
    assert _transactional_wrong_time_writes(record, "23:00") == 0
