from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.structured_output import canonicalize_provider_json_schema
from app.agents.v2.turn_contract import (
    ActiveTaskRelativeDate,
    DateConstraint,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)


def test_v2_turn_schema_is_strict_provider_compatible() -> None:
    schema = TiaTurnUnderstanding.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    provider = canonicalize_provider_json_schema(schema)
    assert "$defs" not in provider
    assert "$ref" not in str(provider)


def test_v2_provider_schema_exposes_financial_ledger_as_pulse_semantic_marker() -> None:
    schema = TiaTurnUnderstanding.model_json_schema()
    pulse_details = schema["$defs"]["TurnOperation"]["properties"]["requested_pulse_details"]
    item_schema = pulse_details["items"]
    assert "financial_ledger" in item_schema["enum"]

    provider = canonicalize_provider_json_schema(schema)
    assert "financial_ledger" in str(provider)


def test_v2_contract_supports_compound_operations_without_capability_fields() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="pricing",
                entities=TurnEntities(),
                selection=None,
                package_usage="unspecified",
            ),
            TurnOperation(
                type="availability",
                entities=TurnEntities(
                    date=DateConstraint(
                        mode="exact",
                        start_date="2026-09-12",
                        end_date=None,
                    )
                ),
                selection=None,
                package_usage="unspecified",
            ),
        ],
        safety_signals=[],
    )
    payload = turn.model_dump()
    assert [item["type"] for item in payload["operations"]] == ["pricing", "availability"]
    forbidden = {
        "domains",
        "capabilities",
        "flow_signal",
        "clear_entity_fields",
        "missing_information",
        "confidence",
        "reason",
        "recommended_handoff_category",
        "recommended_handoff_priority",
    }
    assert forbidden.isdisjoint(payload)


def test_v2_date_time_and_selection_shapes_fail_closed() -> None:
    with pytest.raises(ValidationError):
        DateConstraint(mode="range", start_date="2026-09-12", end_date=None)

    with pytest.raises(ValidationError):
        TimeConstraint(mode="range", start_time="20:00", end_time="18:00")

    with pytest.raises(ValidationError):
        Selection(kind="index", index=0, time=None, ref=None)


def test_time_selection_discards_redundant_provider_coordinates() -> None:
    selection = Selection.model_validate(
        {
            "kind": "time",
            "index": 2,
            "time": "18:00",
            "ref": "opt_2",
            "relative": "next",
            "time_ambiguity": "none",
        }
    )

    assert selection.time == "18:00"
    assert selection.index is None
    assert selection.ref is None
    assert selection.relative is None

    with pytest.raises(ValidationError):
        Selection.model_validate(
            {
                "kind": "time",
                "index": 2,
                "time": "not-a-time",
                "ref": "opt_2",
                "relative": "next",
                "time_ambiguity": "none",
            }
        )


def test_v2_provider_schema_exposes_typed_patient_profile_scope() -> None:
    schema = TiaTurnUnderstanding.model_json_schema()
    details = schema["$defs"]["TurnOperation"]["properties"]["requested_patient_details"]
    assert set(details["items"]["enum"]) == {"name", "phone", "preferred_language"}
    provider = canonicalize_provider_json_schema(schema)
    assert "requested_patient_details" in str(provider)


def test_v2_provider_schema_exposes_typed_package_read_scope() -> None:
    schema = TiaTurnUnderstanding.model_json_schema()
    details = schema["$defs"]["TurnOperation"]["properties"]["requested_package_details"]
    assert set(details["items"]["enum"]) == {"owned", "offers"}
    provider = canonicalize_provider_json_schema(schema)
    assert "requested_package_details" in str(provider)


def test_active_reschedule_explicit_fields_are_latest_message_provenance() -> None:
    operation = TurnOperation(
        type="reschedule",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date", "time"],
        entities=TurnEntities(
            date=DateConstraint(mode="next_available"),
            time=TimeConstraint(mode="exact", start_time="16:00"),
        ),
    )
    assert operation.active_task_explicit_fields == ["date", "time"]

    orphan_date_marker = TurnOperation(
        type="reschedule",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
        entities=TurnEntities(date=None),
    )
    assert orphan_date_marker.active_task_explicit_fields == []

    mixed_markers = TurnOperation(
        type="book",
        active_task_relationship="continue",
        active_task_explicit_fields=["date", "time"],
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="18:00")),
    )
    assert mixed_markers.active_task_explicit_fields == ["time"]

    with pytest.raises(ValidationError):
        TurnOperation(
            type="reschedule",
            active_task_relationship="unspecified",
            active_task_explicit_fields=["date"],
            entities=TurnEntities(date=DateConstraint(mode="next_available")),
        )


def test_non_task_active_task_explicit_fields_are_discarded() -> None:
    operation = TurnOperation(
        type="availability",
        execution_intent="informational",
        active_task_explicit_fields=["date"],
        entities=TurnEntities(date=DateConstraint(mode="exact", start_date="2026-09-12")),
    )

    assert operation.active_task_explicit_fields == []
    assert operation.entities.date == DateConstraint(mode="exact", start_date="2026-09-12")



def test_active_reschedule_relative_date_contract_is_typed_and_fail_closed() -> None:
    relation = ActiveTaskRelativeDate(relation="next_occurrence", weekday="saturday")
    operation = TurnOperation(
        type="reschedule",
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
        active_task_relative_date=relation,
        entities=TurnEntities(),
    )
    assert operation.active_task_relative_date == relation
    assert operation.entities.date is None
    assert operation.active_task_explicit_fields == ["date"]

    with pytest.raises(ValidationError):
        TurnOperation(
            type="reschedule",
            execution_intent="execute",
            active_task_relationship="continue",
            active_task_explicit_fields=["date"],
            active_task_relative_date=relation,
            entities=TurnEntities(
                date=DateConstraint(mode="exact", start_date="2026-10-31")
            ),
        )

    with pytest.raises(ValidationError):
        TurnOperation(
            type="book",
            execution_intent="execute",
            active_task_relationship="continue",
            active_task_explicit_fields=["date"],
            active_task_relative_date=relation,
            entities=TurnEntities(),
        )

    with pytest.raises(ValidationError):
        TurnOperation(
            type="reschedule",
            execution_intent="execute",
            active_task_relationship="continue",
            active_task_relative_date=relation,
            entities=TurnEntities(),
        )


def test_calendar_relative_schema_does_not_change_date_modes_or_selection_relative_semantics() -> None:
    schema = TiaTurnUnderstanding.model_json_schema()
    date_modes = schema["$defs"]["DateConstraint"]["properties"]["mode"]["enum"]
    assert set(date_modes) == {"exact", "range", "from_date", "next_available"}
    relative = schema["$defs"]["ActiveTaskRelativeDate"]
    assert relative["properties"]["relation"]["const"] == "next_occurrence"
    assert set(relative["properties"]["weekday"]["enum"]) == {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
    selection_relative = schema["$defs"]["Selection"]["properties"]["relative"]
    assert set(selection_relative["anyOf"][0]["enum"]) == {
        "next",
        "previous",
        "first",
        "last",
    }


def test_turn_mutation_policy_contract_is_two_value_turn_local_ceiling() -> None:
    schema = TiaTurnUnderstanding.model_json_schema()
    policy = schema["properties"]["turn_mutation_policy"]
    assert set(policy["enum"]) == {"operation_local", "preview_only"}
    assert policy["default"] == "operation_local"
    description = str(policy.get("description") or "").lower()
    assert "maximum mutation authority" in description
    assert "never grants execution" in description


def test_turn_mutation_policy_accepts_preview_only_without_rewriting_operation_intent() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="buy_package",
                entities=TurnEntities(),
                execution_intent="execute",
            )
        ],
        turn_mutation_policy="preview_only",
    )
    assert turn.turn_mutation_policy == "preview_only"
    assert turn.operations[0].execution_intent == "execute"
