from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agents.structured_output import StructuredOutputError, invoke_typed_structured_output
from app.agents.v2 import turn_interpreter as interpreter
from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import TiaTurnUnderstanding, TurnEntities, TurnOperation


class _SyntheticSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["allowed"]
    count: int


class _StructuredInvoker:
    def __init__(self, value: object) -> None:
        self.value = value

    def invoke(self, _messages):
        return self.value


class _FakeModel:
    def __init__(self, value: object) -> None:
        self.value = value

    def with_structured_output(self, **_kwargs):
        return _StructuredInvoker(self.value)


def _invoke(value: object, *, schema: type[BaseModel]) -> object:
    return invoke_typed_structured_output(model=_FakeModel(value), schema=schema, messages=[])


def _rejected_turn_payload(*, operation_types: list[str], sentinel: str) -> dict[str, object]:
    operations: list[dict[str, object]] = []
    for operation_type in operation_types:
        operations.append(
            {
                "type": operation_type,
                "entities": {
                    "doctor": {
                        "text": sentinel,
                        "ref": "SECRET_REF_SHOULD_NOT_LEAK",
                        "candidate_refs": ["SECRET_CANDIDATE_SHOULD_NOT_LEAK"],
                        "candidate_mode": "ambiguous",
                    }
                },
                "execution_intent": "informational",
            }
        )
    return {
        "operations": operations,
        "safety_signals": [],
        "response_disposition": "SECRET_INVALID_DISPOSITION_SHOULD_NOT_LEAK",
    }


def test_structured_validation_error_preserves_safe_bounded_diagnostics() -> None:
    sentinel = "SECRET_INVALID_VALUE_SHOULD_NOT_LEAK"
    with pytest.raises(StructuredOutputError) as caught:
        _invoke({"status": sentinel, "count": 1}, schema=_SyntheticSchema)

    error = caught.value
    assert isinstance(error.__cause__, ValidationError)
    assert error.validation_category == "local_structured_validation"
    assert error.validation_issues == ({"location": "status", "type": "literal_error"},)
    assert str(error) == "LLM provider returned structured data that failed local validation."

    serialized = json.dumps(error.safe_diagnostics(), sort_keys=True)
    assert sentinel not in str(error)
    assert sentinel not in serialized
    assert "input" not in serialized


def test_turn_rejection_diagnostics_observe_doctor_and_pricing_without_entity_text() -> None:
    sentinel = "SECRET_ENTITY_TEXT_SHOULD_NOT_LEAK"
    payload = _rejected_turn_payload(
        operation_types=["doctor_info", "pricing"],
        sentinel=sentinel,
    )
    with pytest.raises(StructuredOutputError) as caught:
        _invoke(payload, schema=TiaTurnUnderstanding)

    diagnostics = interpreter.safe_turn_interpreter_rejection_diagnostics(caught.value)
    shape = diagnostics["rejected_semantic_shape"]
    assert shape["operations"] == [
        {"type": "doctor_info", "execution_intent": "informational"},
        {"type": "pricing", "execution_intent": "informational"},
    ]
    serialized = json.dumps(diagnostics, sort_keys=True)
    assert sentinel not in serialized
    assert "SECRET_REF_SHOULD_NOT_LEAK" not in serialized
    assert "SECRET_CANDIDATE_SHOULD_NOT_LEAK" not in serialized
    assert "doctor_info" in serialized
    assert "pricing" in serialized


def test_turn_rejection_diagnostics_do_not_invent_pricing_for_doctor_only_shape() -> None:
    payload = _rejected_turn_payload(
        operation_types=["doctor_info"],
        sentinel="SECRET_ENTITY_TEXT_SHOULD_NOT_LEAK",
    )
    with pytest.raises(StructuredOutputError) as caught:
        _invoke(payload, schema=TiaTurnUnderstanding)

    diagnostics = interpreter.safe_turn_interpreter_rejection_diagnostics(caught.value)
    serialized = json.dumps(diagnostics, sort_keys=True)
    assert diagnostics["rejected_semantic_shape"]["operations"] == [
        {"type": "doctor_info", "execution_intent": "informational"}
    ]
    assert "pricing" not in serialized


def test_unextractable_shape_reports_not_observable_without_raw_value() -> None:
    sentinel = "SECRET_MALFORMED_PAYLOAD_SHOULD_NOT_LEAK"
    with pytest.raises(StructuredOutputError) as caught:
        _invoke(sentinel, schema=TiaTurnUnderstanding)

    diagnostics = interpreter.safe_turn_interpreter_rejection_diagnostics(caught.value)
    assert diagnostics["validation_category"] == "payload_extraction"
    assert diagnostics["rejected_semantic_shape"] == "not_observable"
    assert sentinel not in json.dumps(diagnostics, sort_keys=True)
    assert sentinel not in str(caught.value)


def _minimal_semantic_turn() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="social",
                entities=TurnEntities(),
                execution_intent="informational",
            )
        ]
    )


def _patch_interpreter_runtime(monkeypatch: pytest.MonkeyPatch, *, primary: object, fallback: object) -> None:
    monkeypatch.setattr(interpreter.settings, "openai_model", "primary-test")
    monkeypatch.setattr(interpreter.settings, "openai_fallback_model", "fallback-test")
    monkeypatch.setattr(interpreter, "build_realtime_interpreter_model", lambda: primary)
    monkeypatch.setattr(interpreter, "build_realtime_interpreter_fallback_model", lambda: fallback)
    monkeypatch.setattr(
        interpreter,
        "_messages_for_prompt_cache",
        lambda _model, messages, **_kwargs: messages,
    )


def test_rejection_diagnostics_do_not_change_primary_to_fallback_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = object()
    fallback = object()
    expected = _minimal_semantic_turn()
    calls: list[str] = []
    errors: list[StructuredOutputError] = []
    _patch_interpreter_runtime(monkeypatch, primary=primary, fallback=fallback)

    def invoke(*, model, **_kwargs):
        if model is primary:
            calls.append("primary")
            error = StructuredOutputError(
                "LLM provider returned structured data that failed local validation.",
                validation_category="local_structured_validation",
                validation_issues=({"location": "response_disposition", "type": "literal_error"},),
                rejected_payload=_rejected_turn_payload(
                    operation_types=["doctor_info", "pricing"],
                    sentinel="SECRET_ENTITY_TEXT_SHOULD_NOT_LEAK",
                ),
            )
            errors.append(error)
            raise error
        calls.append("fallback")
        return expected

    monkeypatch.setattr(interpreter, "invoke_typed_structured_output", invoke)
    result = interpreter.interpret_customer_turn_v2(
        history=[HumanMessage(content="synthetic customer turn")],
        semantic_context=build_semantic_context({"services": [], "doctors": [], "appointments": []}),
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
    )

    assert result == expected
    assert calls == ["primary", "primary", "fallback"]
    assert len(errors) == 2
    for error in errors:
        assert error.rejected_semantic_shape == {
            "operation_count": 2,
            "operations": [
                {"type": "doctor_info", "execution_intent": "informational"},
                {"type": "pricing", "execution_intent": "informational"},
            ],
        }
        assert error.rejected_payload_for_diagnostics is None


def test_successful_primary_remains_single_attempt_without_rejection_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = object()
    fallback = object()
    expected = _minimal_semantic_turn()
    calls: list[str] = []
    _patch_interpreter_runtime(monkeypatch, primary=primary, fallback=fallback)

    def invoke(*, model, **_kwargs):
        calls.append("primary" if model is primary else "fallback")
        return expected

    monkeypatch.setattr(interpreter, "invoke_typed_structured_output", invoke)
    result = interpreter.interpret_customer_turn_v2(
        history=[HumanMessage(content="synthetic customer turn")],
        semantic_context=build_semantic_context({"services": [], "doctors": [], "appointments": []}),
        timezone_name="Africa/Cairo",
        local_now=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
    )

    assert result == expected
    assert calls == ["primary"]
