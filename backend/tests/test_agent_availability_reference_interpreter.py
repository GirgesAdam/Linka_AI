from __future__ import annotations

import json

import pytest

from app.agents.structured_output import StructuredOutputError
from app.agents.v2 import availability_reference_interpreter as resolver
from app.agents.v2.availability_reference_interpreter import (
    ReferenceDecision,
    interpret_availability_reference_turn,
)
from app.agents.v2.turn_contract import Selection
from app.core.config import settings


def _context(*, selected: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "concrete": True,
                "start_time_24h": "10:00",
                "start_local": "2026-10-12T10:00:00+03:00",
                "doctor_name": "Mariam",
                "slot": {"doctor_id": "secret-doctor-id", "start_at": "secret"},
            },
            {
                "option_ref": "opt_2",
                "concrete": True,
                "start_time_24h": "12:00",
                "start_local": "2026-10-12T12:00:00+03:00",
                "doctor_name": "Mariam",
                "slot": {"doctor_id": "secret-doctor-id-2", "start_at": "secret-2"},
            },
            {
                "option_ref": "opt_3",
                "concrete": False,
                "start_time_24h": "15:00",
                "end_time_24h": "17:00",
                "doctor_name": "Mariam",
                "slot": {"doctor_id": "must-never-reach-model"},
            },
        ]
    }
    if selected is not None:
        value["last_selected_option_ref"] = selected
    return value


def test_reference_decision_contract_is_tiny_and_rejects_invalid_ref_shape() -> None:
    assert ReferenceDecision(action="select_presented_option", option_ref="opt_2").option_ref == "opt_2"
    assert ReferenceDecision(action="refresh_availability", option_ref=None).option_ref is None
    exact = ReferenceDecision(action="new_search", option_ref=None, exact_time="03:00")
    assert exact.exact_time == "03:00"
    with pytest.raises(ValueError):
        ReferenceDecision(action="select_presented_option", option_ref=None)
    with pytest.raises(ValueError):
        ReferenceDecision(action="normal", option_ref="opt_1")
    with pytest.raises(ValueError):
        ReferenceDecision(action="select_presented_option", option_ref="opt_1", exact_time="03:00")


def test_presented_option_ref_normalizes_only_redundant_provider_coordinates() -> None:
    selection = Selection.model_validate(
        {
            "kind": "ref",
            "ref": "opt_2",
            "index": 2,
            "time": "12:00",
            "relative": "next",
            "time_ambiguity": "twelve_hour",
        }
    )
    assert selection == Selection(kind="ref", ref="opt_2")

    with pytest.raises(ValueError):
        Selection.model_validate(
            {
                "kind": "ref",
                "ref": "A1",
                "index": 2,
                "time": None,
                "relative": None,
                "time_ambiguity": "none",
            }
        )


def test_model_input_contains_only_displayed_safe_options_and_opaque_refs() -> None:
    messages = resolver._messages(
        latest_customer_text="التاني",
        availability_context=_context(selected="opt_1"),
    )
    payload = json.loads(str(messages[-1].content))
    assert payload["last_selected_option_ref"] == "opt_1"
    assert [item["option_ref"] for item in payload["last_presented_availability_options"]] == [
        "opt_1",
        "opt_2",
        "opt_3",
    ]
    assert payload["last_presented_availability_options"][2]["concrete"] is False
    assert "slot" not in payload["last_presented_availability_options"][0]
    assert "doctor_id" not in payload["last_presented_availability_options"][0]


@pytest.mark.parametrize(
    ("decision", "expected_action", "expected_ref"),
    [
        ({"action": "select_presented_option", "option_ref": "opt_2"}, "select_presented_option", "opt_2"),
        ({"action": "refresh_availability", "option_ref": None}, "refresh_availability", None),
        ({"action": "new_search", "option_ref": None}, "new_search", None),
        ({"action": "clarify", "option_ref": None}, "clarify", None),
        ({"action": "normal", "option_ref": None}, "normal", None),
    ],
)
def test_lightweight_resolver_returns_small_semantic_decisions(
    monkeypatch: pytest.MonkeyPatch,
    decision: dict[str, object],
    expected_action: str,
    expected_ref: str | None,
) -> None:
    model = object()
    monkeypatch.setattr(settings, "openai_model", "primary-test")
    monkeypatch.setattr(settings, "openai_fallback_model", "")
    monkeypatch.setattr(resolver, "build_realtime_interpreter_model", lambda: model)
    monkeypatch.setattr(resolver, "build_realtime_interpreter_fallback_model", lambda: None)
    monkeypatch.setattr(
        resolver,
        "invoke_typed_structured_output",
        lambda **_kwargs: ReferenceDecision.model_validate(decision),
    )

    result = interpret_availability_reference_turn(
        latest_customer_text="natural customer follow-up",
        availability_context=_context(selected="opt_1"),
    )

    assert result.decision.action == expected_action
    assert result.decision.option_ref == expected_ref
    assert result.model_name == "primary-test"
    assert result.structured_output_error is False


def test_schema_failure_is_contained_as_clarification_without_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_model", "primary-test")
    monkeypatch.setattr(settings, "openai_fallback_model", "fallback-test")
    monkeypatch.setattr(resolver, "build_realtime_interpreter_model", lambda: object())
    monkeypatch.setattr(resolver, "build_realtime_interpreter_fallback_model", lambda: object())

    calls = 0

    def fail_structured(**_kwargs):
        nonlocal calls
        calls += 1
        raise StructuredOutputError("invalid local schema")

    monkeypatch.setattr(resolver, "invoke_typed_structured_output", fail_structured)

    result = interpret_availability_reference_turn(
        latest_customer_text="التاني",
        availability_context=_context(),
    )

    assert calls == 4  # two bounded schema attempts on primary + fallback
    assert result.decision == ReferenceDecision(action="clarify", option_ref=None)
    assert result.model_name is None
    assert result.structured_output_error is True


def test_primary_schema_failure_can_recover_on_fallback_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = object()
    fallback = object()
    monkeypatch.setattr(settings, "openai_model", "primary-test")
    monkeypatch.setattr(settings, "openai_fallback_model", "fallback-test")
    monkeypatch.setattr(resolver, "build_realtime_interpreter_model", lambda: primary)
    monkeypatch.setattr(resolver, "build_realtime_interpreter_fallback_model", lambda: fallback)

    def invoke(*, model, **_kwargs):
        if model is primary:
            raise StructuredOutputError("primary invalid")
        return ReferenceDecision(action="select_presented_option", option_ref="opt_2")

    monkeypatch.setattr(resolver, "invoke_typed_structured_output", invoke)

    result = interpret_availability_reference_turn(
        latest_customer_text="التاني",
        availability_context=_context(),
    )

    assert result.decision.option_ref == "opt_2"
    assert result.model_name == "fallback-test"
    assert result.structured_output_error is True
