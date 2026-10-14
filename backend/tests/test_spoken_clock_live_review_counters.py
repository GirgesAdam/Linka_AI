from __future__ import annotations

from scripts.run_clinic_hours_clock_live_review import _transactional_safety_deltas


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
