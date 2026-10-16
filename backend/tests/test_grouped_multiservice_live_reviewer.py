from __future__ import annotations

import copy

import pytest

from scripts.grouped_multiservice_intent_review_contract import validate_g6_visit_time_selection


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
                "date": {"mode": "exact", "value": "2026-10-17"},
                "time": {"mode": "exact", "value": "20:00"},
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
                "facts": {"availability": {"available_option_count": 1}},
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


def test_ra_g6_reviewer_accepts_unified_booking_proposal_contract() -> None:
    payload = _valid_payload()
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
