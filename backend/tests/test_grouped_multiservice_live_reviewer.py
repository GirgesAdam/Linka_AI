from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException

import scripts.run_grouped_multiservice_intent_live_review as live_review
from app.agents.structured_output import StructuredOutputError
from app.services.agent_v2.orchestrator import V2RuntimeStepTrace
from scripts.grouped_multiservice_intent_review_contract import (
    _fresh_slots_matching_proposal,
    validate_g6_visit_time_selection,
)
from scripts.run_grouped_multiservice_intent_live_review import turn_record, validate_g5


def _valid_payload() -> dict[str, object]:
    underarm = "svc-underarm"
    bikini = "svc-bikini"
    doctor = "doc-maryam"
    branch = "branch-main"
    start = "2026-10-17T20:00:00+03:00"
    expected = [underarm, bikini]
    context = {
        "requested_service_ids": expected,
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "concrete": True,
                "canonical_slot_count": 1,
                "start_local": start,
                "doctor_id": doctor,
                "device_key": "candela_gentle",
            }
        ],
    }
    base_record = {
        "requested_service_ids": expected,
        "pending_write": None,
        "traces": [],
        "availability_reference_context": context,
    }
    final = {
        **copy.deepcopy(base_record),
        "reference_action": "select_presented_option",
        "selected_option_ref": "opt_1",
        "full_interpreter_called": False,
        "active_task": {
            "status": "awaiting_confirmation",
            "requested_service_ids": expected,
            "derived": {"selected_slot_ref": "opt_1"},
            "constraints": {
                "doctor_id": doctor,
                "branch_id": branch,
                "device_key": "candela_gentle",
                "date": {"mode": "exact", "start_date": "2026-10-17", "end_date": None},
                "time": {"mode": "exact", "start_time": "20:00", "end_time": None},
            },
            "service_device_keys": {
                underarm: "candela_gentle",
                bikini: "candela_gentle",
            },
        },
        "proposal": {
            "service_ids": expected,
            "main_service_id": underarm,
            "additional_service_ids": [bikini],
            "branch_id": branch,
            "doctor_id": doctor,
            "device_key": "candela_gentle",
            "additional_service_device_keys": {bikini: "candela_gentle"},
            "start_at": start,
            "end_at": "2026-10-17T20:30:00+03:00",
            "visits": [],
        },
        "traces": [
            {
                "read_kinds": ["availability"],
                "pending_write": False,
                "availability_slots": [
                    {
                        "branch_id": branch,
                        "doctor_id": doctor,
                        "laser_device_key": "candela_gentle",
                        "start_at": start,
                    }
                ],
            }
        ],
        "outcomes": [
            {
                "response_goal": "booking_proposal",
                "facts": {
                    "exact_time_requested": True,
                    "verified_booking_slot_selection": True,
                    "verified_availability_reference": True,
                    "verified_selection_time": {"mode": "exact", "start_time": "20:00"},
                    "availability": {"available_option_count": 1},
                },
            }
        ],
        "reply": (
            "\u0627\u0644\u0645\u0648\u0639\u062f \u0627\u0644\u0645\u0642\u062a\u0631\u062d: "
            "\u0644\u064a\u0632\u0631 \u0625\u0628\u0637 + \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a "
            "\u0645\u0639 \u062f. \u0645\u0631\u064a\u0645 \u0639\u0644\u0649 \u062c\u0647\u0627\u0632 Candela Gentle. "
            "\u062a\u062d\u0628\u064a \u062a\u0623\u0643\u062f\u064a \u0627\u0644\u062d\u062c\u0632 \u0648\u0644\u0627 \u062a\u0639\u062f\u0651\u0644\u064a \u062d\u0627\u062c\u0629\u061f"
        ),
    }
    return {
        "ids": {"underarm": underarm, "bikini": bikini, "doctor": doctor},
        "records": [copy.deepcopy(base_record), copy.deepcopy(base_record), final],
    }


def test_grouped_capture_adds_safe_structured_rejection_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "SECRET_ENTITY_TEXT_SHOULD_NOT_LEAK"
    error = StructuredOutputError(
        "LLM provider returned structured data that failed local validation.",
        validation_category="local_structured_validation",
        validation_issues=({"location": "response_disposition", "type": "literal_error"},),
        rejected_payload={
            "operations": [
                {
                    "type": "doctor_info",
                    "execution_intent": "informational",
                    "entities": {"doctor": {"text": sentinel, "ref": "SECRET_ID"}},
                },
                {
                    "type": "pricing",
                    "execution_intent": "informational",
                    "entities": {},
                },
            ],
            "response_disposition": "invalid-secret-value",
        },
    )

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(live_review, "_original_invoke", fail)
    live_review.RAW.clear()
    with pytest.raises(StructuredOutputError):
        live_review.capture_invoke(model=SimpleNamespace(model_name="primary-test"))

    assert len(live_review.RAW) == 1
    record = live_review.RAW[0]
    assert record["status"] == "error"
    assert record["model"] == "primary-test"
    assert record["error_type"] == "StructuredOutputError"
    assert record["validation_category"] == "local_structured_validation"
    assert record["validation_issues"] == [
        {"location": "response_disposition", "type": "literal_error"}
    ]
    assert record["rejected_semantic_shape"] == {
        "operation_count": 2,
        "operations": [
            {"type": "doctor_info", "execution_intent": "informational"},
            {"type": "pricing", "execution_intent": "informational"},
        ],
    }
    serialized = json.dumps(record, sort_keys=True)
    assert sentinel not in serialized
    assert "SECRET_ID" not in serialized
    assert error.rejected_payload_for_diagnostics is None


def test_grouped_capture_redacts_unextractable_parser_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "SECRET_RAW_MODEL_OUTPUT_SHOULD_NOT_LEAK"
    error = OutputParserException(sentinel, llm_output=sentinel)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(live_review, "_original_invoke", fail)
    live_review.RAW.clear()
    with pytest.raises(OutputParserException):
        live_review.capture_invoke(model=SimpleNamespace(model_name="primary-test"))

    assert len(live_review.RAW) == 1
    record = live_review.RAW[0]
    assert record == {
        "status": "error",
        "model": "primary-test",
        "error_type": "OutputParserException",
        "error": "LLM provider returned structured data that could not be parsed.",
        "validation_category": "payload_extraction",
        "rejected_semantic_shape": "not_observable",
    }
    assert sentinel not in json.dumps(record, sort_keys=True)


def test_grouped_live_review_preserves_trace_for_fresh_slot_matching() -> None:
    start = "2026-10-17T20:00:00+03:00"
    trace = V2RuntimeStepTrace(
        operation_index=0,
        operation_type="availability",
        disposition_before="read",
        disposition_after="read",
        read_kinds=("availability",),
        availability_slots=(
            {
                "branch_id": "branch-main",
                "doctor_id": "doc-maryam",
                "laser_device_key": "candela_gentle",
                "start_at": start,
            },
        ),
    )
    turn = SimpleNamespace(
        understanding=None,
        active_task=None,
        plan=None,
        traces=(trace,),
        outcomes=(),
        pending_write=None,
        reply=None,
        responder_model=None,
        reference_action=None,
        selected_option_ref=None,
        full_interpreter_called=False,
    )
    record = turn_record(turn, customer="test", raw=[], stages=[], active_task_before=None)
    proposal = {
        "branch_id": "branch-main",
        "doctor_id": "doc-maryam",
        "device_key": "candela_gentle",
        "start_at": start,
    }

    assert isinstance(record["traces"][0], dict)
    assert record["traces"][0]["availability_slots"][0]["start_at"] == start
    matches = _fresh_slots_matching_proposal(record, proposal)
    assert len(matches) == 1
    assert matches[0]["start_at"] == start


def test_ra_g6_reviewer_accepts_concrete_slot_with_fresh_exact_selection() -> None:
    payload = _valid_payload()
    validate_g6_visit_time_selection(payload["records"], payload["ids"])


def _as_unpresented_new_search(payload: dict[str, object]) -> None:
    first = payload["records"][0]
    option = first["availability_reference_context"]["availability_reference_options"][0]
    option["start_local"] = "2026-10-17T10:00:00+03:00"
    final = payload["records"][-1]
    final["reference_action"] = "new_search"
    final["selected_option_ref"] = None
    final["full_interpreter_called"] = True
    final["active_task"]["derived"]["selected_slot_ref"] = None
    facts = final["outcomes"][0]["facts"]
    facts.pop("verified_booking_slot_selection", None)
    facts.pop("verified_availability_reference", None)
    facts.pop("verified_selection_time", None)


def test_ra_g6_reviewer_accepts_unpresented_exact_time_new_search_with_valid_proposal() -> None:
    payload = _valid_payload()
    _as_unpresented_new_search(payload)

    validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_new_search_when_target_time_was_presented() -> None:
    payload = _valid_payload()
    final = payload["records"][-1]
    final["reference_action"] = "new_search"
    final["selected_option_ref"] = None
    final["full_interpreter_called"] = True

    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_selection_of_unpresented_exact_time() -> None:
    payload = _valid_payload()
    option = payload["records"][0]["availability_reference_context"]["availability_reference_options"][0]
    option["start_local"] = "2026-10-17T10:00:00+03:00"

    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_new_search_without_final_proposal() -> None:
    payload = _valid_payload()
    _as_unpresented_new_search(payload)
    payload["records"][-1]["proposal"] = None

    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_wrong_exact_date() -> None:
    payload = _valid_payload()
    payload["records"][-1]["active_task"]["constraints"]["date"]["start_date"] = "2026-10-18"
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_non_exact_date_mode() -> None:
    payload = _valid_payload()
    payload["records"][-1]["active_task"]["constraints"]["date"]["mode"] = "range"
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])

def test_ra_g6_reviewer_rejects_wrong_exact_time() -> None:
    payload = _valid_payload()
    payload["records"][-1]["active_task"]["constraints"]["time"]["start_time"] = "21:00"
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_non_exact_time_mode() -> None:
    payload = _valid_payload()
    payload["records"][-1]["active_task"]["constraints"]["time"]["mode"] = "window"
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_accepts_window_with_fresh_exact_selection() -> None:
    payload = _valid_payload()
    option = payload["records"][0]["availability_reference_context"]["availability_reference_options"][0]
    option["concrete"] = False
    option["canonical_slot_count"] = 25
    option["start_local"] = "2026-10-17T10:00:00+03:00"
    option["end_local"] = "2026-10-17T22:00:00+03:00"
    validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_window_without_exact_selection() -> None:
    payload = _valid_payload()
    option = payload["records"][0]["availability_reference_context"]["availability_reference_options"][0]
    option["concrete"] = False
    option["canonical_slot_count"] = 25
    facts = payload["records"][-1]["outcomes"][0]["facts"]
    facts["exact_time_requested"] = False
    facts.pop("verified_selection_time")
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_missing_fresh_verification() -> None:
    payload = _valid_payload()
    payload["records"][-1]["traces"] = []
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_proposal_time_mismatch() -> None:
    payload = _valid_payload()
    payload["records"][-1]["proposal"]["start_at"] = "2026-10-17T21:00:00+03:00"
    payload["records"][-1]["proposal"]["end_at"] = "2026-10-17T21:30:00+03:00"
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_service_loss() -> None:
    payload = _valid_payload()
    payload["records"][-1]["proposal"]["service_ids"] = ["svc-underarm"]
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_wrong_device() -> None:
    payload = _valid_payload()
    payload["records"][-1]["proposal"]["device_key"] = "prime_lase"
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def test_ra_g6_reviewer_rejects_false_success_reply() -> None:
    payload = _valid_payload()
    payload["records"][-1]["reply"] = "\u062a\u0645 \u0627\u0644\u062d\u062c\u0632 \u0628\u0646\u062c\u0627\u062d"
    with pytest.raises(AssertionError):
        validate_g6_visit_time_selection(payload["records"], payload["ids"])


def _g5_payload(*, final_time: str = "18:00", final_start_at: str = "2026-10-17T15:00:00+00:00"):
    ids = {"underarm": "svc-underarm", "bikini": "svc-bikini", "doctor": "doc-maryam"}
    expected = [ids["underarm"], ids["bikini"]]

    def task(time_value: str):
        return {
            "status": "awaiting_confirmation",
            "requested_service_ids": expected,
            "constraints": {"time": {"mode": "exact", "start_time": time_value}},
            "write_authorization": {"authorized": True},
            "derived": {"commercial_basis_presented_key": "svc-underarm|candela_gentle|standalone"},
        }

    def proposal(start_at: str):
        return {
            "service_ids": expected,
            "main_service_id": ids["underarm"],
            "additional_service_ids": [ids["bikini"]],
            "doctor_id": ids["doctor"],
            "device_key": "candela_gentle",
            "start_at": start_at,
        }

    records = [
        {"requested_service_ids": expected, "active_task": {"requested_service_ids": expected}, "pending_write": None},
        {
            "requested_service_ids": expected,
            "active_task": task("18:00"),
            "proposal": proposal("2026-10-17T15:00:00+00:00"),
            "pending_write": None,
            "reply": "السعر الإجمالي 1000 جنيه. الموعد المقترح قبل التأكيد.",
        },
        {
            "requested_service_ids": expected,
            "active_task": task(final_time),
            "proposal": proposal(final_start_at),
            "pending_write": None,
            "reply": "الموعد المقترح جاهز قبل التأكيد النهائي.",
        },
    ]
    return records, ids


def test_ra_g5_reviewer_accepts_proposal_only_progression_after_commercial_verification() -> None:
    records, ids = _g5_payload()
    validate_g5(records, ids)


def test_ra_g5_reviewer_accepts_corrected_time_with_fresh_proposal() -> None:
    records, ids = _g5_payload(
        final_time="19:00",
        final_start_at="2026-10-17T16:00:00+00:00",
    )
    validate_g5(records, ids)


def test_ra_g5_reviewer_rejects_stale_proposal_after_time_correction() -> None:
    records, ids = _g5_payload(
        final_time="19:00",
        final_start_at="2026-10-17T15:00:00+00:00",
    )
    with pytest.raises(AssertionError):
        validate_g5(records, ids)

def test_ra_g5_reviewer_rejects_missing_durable_commercial_evidence() -> None:
    records, ids = _g5_payload()
    records[1]["active_task"]["derived"]["commercial_basis_presented_key"] = None
    with pytest.raises(AssertionError):
        validate_g5(records, ids)


def test_ra_g5_reviewer_rejects_transient_marker_without_durable_commercial_evidence() -> None:
    records, ids = _g5_payload()
    records[1]["active_task"]["derived"]["commercial_basis_presented_key"] = None
    records[1]["outcomes"] = [{"facts": {"booking_commercial_basis_step": True}}]
    with pytest.raises(AssertionError):
        validate_g5(records, ids)


def test_ra_g5_reviewer_rejects_time_only_correction_that_loses_commercial_authority() -> None:
    records, ids = _g5_payload(
        final_time="19:00",
        final_start_at="2026-10-17T16:00:00+00:00",
    )
    records[2]["active_task"]["derived"]["commercial_basis_presented_key"] = None
    with pytest.raises(AssertionError):
        validate_g5(records, ids)
