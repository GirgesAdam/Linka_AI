from __future__ import annotations

import pytest

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_presented_availability_context


def _context(*, action: str, time_constraint: dict[str, object] | None = None) -> SemanticContext:
    return SemanticContext(
        model_input={
            "availability_followup_intent": {
                "action": action,
                "time_constraint": time_constraint,
            },
            "presented_availability": {
                "service_ref": "S19",
                "doctor_ref": "D2",
                "device_ref": "V1",
                "date": {
                    "mode": "exact",
                    "start_date": "2026-10-12",
                    "end_date": None,
                },
                "package_usage": "unspecified",
                "options": [
                    {
                        "option_ref": "opt_1",
                        "concrete": False,
                        "start_local": "2026-10-12T10:00:00+03:00",
                        "end_local": "2026-10-12T17:00:00+03:00",
                    }
                ],
            },
        },
        reference_map={},
    )


def test_refresh_availability_restores_server_owned_scope_after_side_read() -> None:
    model_turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(
                    date=DateConstraint(mode="next_available"),
                ),
                execution_intent="informational",
            )
        ]
    )

    result = merge_presented_availability_context(
        model_turn,
        _context(action="refresh_availability"),
    )
    operation = result.operations[0]

    assert operation.continues_previous is True
    assert operation.fresh_task is False
    assert operation.entities.service == EntityReference(ref="S19")
    assert operation.entities.doctor == EntityReference(ref="D2")
    assert operation.entities.device == EntityReference(ref="V1")
    assert operation.entities.date == DateConstraint(
        mode="exact",
        start_date="2026-10-12",
    )


def test_new_availability_search_does_not_inherit_old_presented_scope() -> None:
    new_date = DateConstraint(mode="exact", start_date="2026-10-13")
    model_turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(date=new_date),
                execution_intent="informational",
            )
        ]
    )

    result = merge_presented_availability_context(
        model_turn,
        _context(action="new_search"),
    )
    operation = result.operations[0]

    assert operation.entities.service is None
    assert operation.entities.doctor is None
    assert operation.entities.device is None
    assert operation.entities.date == new_date
    assert operation.continues_previous is False


def test_refresh_does_not_overwrite_explicit_semantic_corrections() -> None:
    explicit_date = DateConstraint(mode="exact", start_date="2026-10-14")
    explicit_time = TimeConstraint(mode="exact", start_time="17:00")
    model_turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(
                    service=EntityReference(ref="S99"),
                    doctor=EntityReference(ref="D99"),
                    date=explicit_date,
                    time=explicit_time,
                ),
                package_usage="avoid_existing",
                execution_intent="informational",
            )
        ]
    )

    result = merge_presented_availability_context(
        model_turn,
        _context(action="refresh_availability"),
    )
    operation = result.operations[0]

    assert operation.entities.service == EntityReference(ref="S99")
    assert operation.entities.doctor == EntityReference(ref="D99")
    assert operation.entities.date == explicit_date
    assert operation.entities.time == explicit_time
    assert operation.package_usage == "avoid_existing"


@pytest.mark.parametrize(
    ("mode", "start_time"),
    [
        ("nearest", None),
        ("before", "18:00"),
        ("after", "18:00"),
    ],
)
def test_new_search_applies_typed_relative_time_delta_and_preserves_scope(
    mode: str,
    start_time: str | None,
) -> None:
    time_constraint = {
        "mode": mode,
        "start_time": start_time,
        "end_time": None,
        "start_time_ambiguity": "none",
        "end_time_ambiguity": "none",
    }
    model_turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(),
                execution_intent="informational",
                continues_previous=True,
            )
        ]
    )

    result = merge_presented_availability_context(
        model_turn,
        _context(action="new_search", time_constraint=time_constraint),
    )
    operation = result.operations[0]

    assert operation.continues_previous is True
    assert operation.fresh_task is False
    assert operation.entities.service == EntityReference(ref="S19")
    assert operation.entities.doctor == EntityReference(ref="D2")
    assert operation.entities.device == EntityReference(ref="V1")
    assert operation.entities.date == DateConstraint(
        mode="exact",
        start_date="2026-10-12",
    )
    assert operation.entities.time == TimeConstraint(
        mode=mode,
        start_time=start_time,
        start_time_ambiguity="none",
    )
