from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.structured_output import canonicalize_provider_json_schema
from app.agents.v2.turn_contract import (
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

    with pytest.raises(ValidationError):
        TurnOperation(
            type="reschedule",
            active_task_relationship="continue",
            active_task_explicit_fields=["date"],
            entities=TurnEntities(date=None),
        )

    with pytest.raises(ValidationError):
        TurnOperation(
            type="reschedule",
            active_task_relationship="unspecified",
            active_task_explicit_fields=["date"],
            entities=TurnEntities(date=DateConstraint(mode="next_available")),
        )
