from __future__ import annotations

import app.agents.v2.turn_contract as contract
import app.services.agent_v2.orchestrator as runtime


_RECENT_DATE_LEVEL_MISS = {
    "operation_type": "book",
    "availability_option_count": 0,
    "date": {"mode": "exact", "start_date": "2026-10-05", "end_date": None},
}


class _AvailabilityResult:
    kind = "availability"
    ok = True

    def __init__(self, count: int) -> None:
        self.payload = {"matching_slot_count": count}


def _availability_bundle(count: int) -> runtime.ReadExecutionBundle:
    return runtime.ReadExecutionBundle(results=[_AvailabilityResult(count)])


def test_cross_turn_conditional_fallback_uses_recent_verified_no_availability() -> None:
    operation = contract.TurnOperation(
        type="availability",
        entities=contract.TurnEntities(),
        continues_previous=True,
        continuation_condition="if_previous_no_availability",
    )
    assert runtime._continuation_condition_satisfied(
        operation,
        previous_reads=None,
        recent_read_context={
            "operation_type": "availability",
            "availability_option_count": 0,
            "date": "2026-10-05",
        },
    ) is True


def test_cross_turn_conditional_fallback_fails_closed_without_verified_zero() -> None:
    operation = contract.TurnOperation(
        type="availability",
        entities=contract.TurnEntities(),
        continues_previous=True,
        continuation_condition="if_previous_no_availability",
    )
    for recent in (
        None,
        {},
        {"operation_type": "availability", "availability_option_count": 1},
        {"operation_type": "clinic_info", "availability_option_count": 0},
        {"operation_type": "availability", "availability_option_count": True},
    ):
        assert runtime._continuation_condition_satisfied(
            operation,
            previous_reads=None,
            recent_read_context=recent,
        ) is False


def test_same_turn_availability_evidence_takes_priority_over_recent_context() -> None:
    operation = contract.TurnOperation(
        type="availability",
        entities=contract.TurnEntities(),
        continues_previous=True,
        continuation_condition="if_previous_no_availability",
    )
    assert runtime._continuation_condition_satisfied(
        operation,
        previous_reads=_availability_bundle(1),
        recent_read_context={
            "operation_type": "availability",
            "availability_option_count": 0,
        },
    ) is False


def test_n1_same_date_continuation_without_forward_semantics_does_not_advance() -> None:
    understanding = contract.TiaTurnUnderstanding(
        operations=[
            contract.TurnOperation(
                type="continue_active",
                entities=contract.TurnEntities(
                    date=contract.DateConstraint(mode="exact", start_date="2026-10-05"),
                ),
                continues_previous=True,
                continuation_condition="always",
            )
        ]
    )

    normalized = runtime._normalize_cross_turn_nearest_after_verified_miss(
        understanding,
        recent_read_context=_RECENT_DATE_LEVEL_MISS,
    )

    assert normalized == understanding
    date_constraint = normalized.operations[0].entities.date
    assert date_constraint is not None
    assert date_constraint.mode == "exact"
    assert date_constraint.start_date == "2026-10-05"


def test_n2_typed_nearest_still_advances_after_verified_date_level_miss() -> None:
    understanding = contract.TiaTurnUnderstanding(
        operations=[
            contract.TurnOperation(
                type="continue_active",
                entities=contract.TurnEntities(
                    date=contract.DateConstraint(mode="exact", start_date="2026-10-05"),
                    time=contract.TimeConstraint(mode="nearest"),
                ),
                continues_previous=True,
                continuation_condition="always",
            )
        ]
    )

    normalized = runtime._normalize_cross_turn_nearest_after_verified_miss(
        understanding,
        recent_read_context=_RECENT_DATE_LEVEL_MISS,
    )

    date_constraint = normalized.operations[0].entities.date
    assert date_constraint is not None
    assert date_constraint.mode == "from_date"
    assert date_constraint.start_date == "2026-10-06"
    time_constraint = normalized.operations[0].entities.time
    assert time_constraint is not None
    assert time_constraint.mode == "nearest"


def test_nearest_does_not_advance_from_positive_recent_availability() -> None:
    understanding = contract.TiaTurnUnderstanding(
        operations=[
            contract.TurnOperation(
                type="continue_active",
                entities=contract.TurnEntities(
                    date=contract.DateConstraint(mode="exact", start_date="2026-10-05"),
                    time=contract.TimeConstraint(mode="nearest"),
                ),
                continues_previous=True,
            )
        ]
    )
    assert runtime._normalize_cross_turn_nearest_after_verified_miss(
        understanding,
        recent_read_context={
            "operation_type": "availability",
            "availability_option_count": 2,
            "date": {"mode": "exact", "start_date": "2026-10-05", "end_date": None},
        },
    ) == understanding


def test_nearest_does_not_override_time_scoped_miss_or_different_date() -> None:
    understanding = contract.TiaTurnUnderstanding(
        operations=[
            contract.TurnOperation(
                type="continue_active",
                entities=contract.TurnEntities(
                    date=contract.DateConstraint(mode="exact", start_date="2026-10-06"),
                    time=contract.TimeConstraint(mode="nearest"),
                ),
                continues_previous=True,
            )
        ]
    )
    for recent in (
        {
            "operation_type": "availability",
            "availability_option_count": 0,
            "date": {"mode": "exact", "start_date": "2026-10-05", "end_date": None},
            "time": {"mode": "after", "start_time": "19:00"},
        },
        _RECENT_DATE_LEVEL_MISS,
    ):
        assert runtime._normalize_cross_turn_nearest_after_verified_miss(
            understanding,
            recent_read_context=recent,
        ) == understanding
