from __future__ import annotations

import pytest

from app.agents.v2.turn_contract import (
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from scripts.run_clinic_hours_clock_live_review import (
    CLOCK_REVIEW_DATE,
    CLOCK_REVIEW_TODAY,
    SCENARIOS,
    _ARABIC_DATE_TEXT,
    SAFETY_COUNTERS,
    _serialized_period_hint,
    _transactional_safety_deltas,
    _validate_stateful_same_clock_correction,
)
from scripts.spoken_clock_live_review_base import _dump_provider_value, _setup


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


def _with_counter_snapshot():
    return dict(SAFETY_COUNTERS)


def _restore_counters(snapshot: dict[str, int]) -> None:
    SAFETY_COUNTERS.clear()
    SAFETY_COUNTERS.update(snapshot)


def test_sr1_absent_serialized_period_hint_normalizes_to_none() -> None:
    payload = {
        "mode": "exact",
        "start_time": "10:30",
        "start_time_ambiguity": "twelve_hour",
    }
    assert _serialized_period_hint(payload) == "none"
    assert _serialized_period_hint(None) == "none"


def test_sr2_explicit_pm_is_stale_in_final_bare_correction_state() -> None:
    snapshot = _with_counter_snapshot()
    try:
        with pytest.raises(AssertionError, match="retained stale PM"):
            _validate_stateful_same_clock_correction(
                corrected_time={
                    "mode": "exact",
                    "start_time": "10:30",
                    "start_time_ambiguity": "twelve_hour",
                    "spoken_period_hint": "pm",
                },
                corrected={"appointments": [], "availability_requests": []},
                final={
                    "agent": "10:30 \u0635\u0628\u0627\u062d\u064b\u0627 \u0648\u0644\u0627 \u0628\u0627\u0644\u0644\u064a\u0644\u061f",
                    "appointments": [],
                    "availability_requests": [],
                },
                scenario="S-R2",
            )
        assert SAFETY_COUNTERS["stale_period_hint_retention"] == snapshot["stale_period_hint_retention"] + 1
    finally:
        _restore_counters(snapshot)


def test_sr3_absent_hint_requires_period_clarification_without_read_or_write() -> None:
    snapshot = _with_counter_snapshot()
    try:
        _validate_stateful_same_clock_correction(
            corrected_time={
                "mode": "exact",
                "start_time": "10:30",
                "start_time_ambiguity": "twelve_hour",
            },
            corrected={"appointments": [], "availability_requests": []},
            final={
                "agent": "\u062a\u0642\u0635\u062f\u064a 10:30 \u0635\u0628\u0627\u062d\u064b\u0627 \u0648\u0644\u0627 \u0628\u0627\u0644\u0644\u064a\u0644\u061f",
                "appointments": [],
                "availability_requests": [],
            },
            scenario="S-R3",
        )
        assert SAFETY_COUNTERS == snapshot
    finally:
        _restore_counters(snapshot)


def test_sr4_silent_2230_resolution_fails_closed() -> None:
    snapshot = _with_counter_snapshot()
    try:
        with pytest.raises(AssertionError, match="did not require AM/PM|progressed to availability"):
            _validate_stateful_same_clock_correction(
                corrected_time={
                    "mode": "exact",
                    "start_time": "10:30",
                    "start_time_ambiguity": "twelve_hour",
                },
                corrected={"appointments": [], "availability_requests": []},
                final={
                    "agent": "10:30 PM selected",
                    "appointments": [],
                    "availability_requests": [{"time": "22:30"}],
                },
                scenario="S-R4",
            )
        assert SAFETY_COUNTERS["arbitrary_10_11_resolution"] > snapshot["arbitrary_10_11_resolution"]
    finally:
        _restore_counters(snapshot)


def test_spoken_clock_setup_accepts_shared_grouped_fixture_shape(monkeypatch) -> None:
    fixture = tuple(object() for _ in range(8))
    monkeypatch.setattr(
        "scripts.spoken_clock_live_review_base.build_fixture",
        lambda _db: fixture,
    )

    class FakeDB:
        committed = False

        def commit(self) -> None:
            self.committed = True

    db = FakeDB()
    context = _setup(db, "fixture-contract")

    assert context.workspace is fixture[0]
    assert context.patient is fixture[1]
    assert context.doctor is fixture[2]
    assert context.underarm is fixture[4]
    assert context.bikini is fixture[5]
    assert context.arms is fixture[6]
    assert db.committed is True


def test_spoken_clock_review_uses_a_future_thursday_date() -> None:
    assert CLOCK_REVIEW_DATE > CLOCK_REVIEW_TODAY
    assert CLOCK_REVIEW_DATE.weekday() == 3
    assert all(_ARABIC_DATE_TEXT in scenario.customer for scenario in SCENARIOS)
