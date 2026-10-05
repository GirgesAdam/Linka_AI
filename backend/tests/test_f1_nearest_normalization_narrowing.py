from __future__ import annotations

from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2 import orchestrator as runtime


_RECENT_DATE_LEVEL_MISS = {
    "operation_type": "book",
    "availability_option_count": 0,
    "date": {"mode": "exact", "start_date": "2026-10-05", "end_date": None},
}


def test_n1_same_date_continuation_without_forward_semantics_does_not_advance() -> None:
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                entities=TurnEntities(
                    date=DateConstraint(mode="exact", start_date="2026-10-05"),
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
    understanding = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                entities=TurnEntities(
                    date=DateConstraint(mode="exact", start_date="2026-10-05"),
                    time=TimeConstraint(mode="nearest"),
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
