from __future__ import annotations

import json
from pathlib import Path

import scripts.run_full_agent_evidence_audit as audit
import scripts.run_grouped_multiservice_intent_live_review as grouped


def test_core_inventory_is_17_conversations_33_turns_and_selector_complete():
    specs = audit.scenario_inventory()
    payload = audit.inventory_payload(specs)

    assert payload["unique_conversations_selected"] == 17
    assert payload["turns_planned"] == 33
    assert payload["duplicates_intentionally_executed"] == 0
    assert payload["selector_invariant"] is True
    assert payload["c7"] == "NOT_INCLUDED"
    assert payload["phase_3_3"] == "NOT_RUN"
    assert [item.scenario_id for item in specs] == list(audit.CORE_ORDER)
    assert set(payload["selected_handler_ids"]) == set(payload["executable_handler_ids"])
    assert len(set(payload["selected_handler_ids"])) == 17
    assert not any("C7" in item for item in payload["selected_handler_ids"])


def test_core_inventory_turn_shape_matches_approved_portfolio():
    turns = {
        item.scenario_name: item.number_of_turns_planned
        for item in audit.scenario_inventory()
    }
    assert turns == {
        "Clinic Info": 2,
        "Simple Service Price": 1,
        "Package Information": 1,
        "Doctor Discovery": 2,
        "Device Selection / Continuity": 2,
        "Nearest Availability + Natural Continuation": 2,
        "Normal Booking + Verified Confirmation": 3,
        "Time Correction During Booking": 2,
        "Service Correction": 2,
        "Side Question During Active Booking": 3,
        "Two Services Same Visit": 2,
        "Reschedule Existing Appointment": 2,
        "Cancel Existing Appointment": 2,
        "Attendance Confirmation": 1,
        "Package Purchase": 2,
        "Package Correction / Recovery": 2,
        "Book Using Existing Package": 2,
    }


def test_execution_dispatch_covers_all_17_selected_handlers():
    specs = audit.scenario_inventory()
    human_grouped = [item for item in specs if item.runner_kind in {"human", "grouped"}]
    v2 = [item for item in specs if item.runner_kind == "v2"]

    assert len(human_grouped) == 2
    assert len(v2) == 15
    assert {item.scenario_id for item in human_grouped + v2} == set(audit.CORE_ORDER)


def test_grouped_core_selector_invokes_exact_scenario_without_mode_dependency(monkeypatch):
    calls: list[tuple[str, list[str], object]] = []

    def fake_run(name, messages, validator):
        calls.append((name, list(messages), validator))

    monkeypatch.setattr(grouped, "run_scenario", fake_run)
    monkeypatch.setenv("RA_G_MODE", "standalone")

    grouped.run_audit_scenario("CORE-TWO-SERVICES-SAME-VISIT")

    assert len(calls) == 1
    name, messages, validator = calls[0]
    assert name == "CORE-TWO-SERVICES-SAME-VISIT"
    assert messages == [grouped.ORIGINAL, grouped.CORE_SAME_VISIT_CONTINUE]
    assert validator is grouped.validate_g1


def test_assert_recorder_captures_behavioral_mismatches_without_stopping_execution():
    source = "x = 1\nassert x == 2, 'legacy mismatch'\ny = 3\nassert y == 3\n"
    namespace = {"_audit_eval_check": audit._audit_eval_check}
    audit._AUDIT_CHECKS.clear()

    exec(audit._compile_with_recorded_asserts(source, source_name="unit.py"), namespace)

    assert namespace["y"] == 3
    assert [row["condition_result"] for row in audit._AUDIT_CHECKS] == [False, True]
    assert audit._AUDIT_CHECKS[0]["legacy_error_if_false"] == "legacy mismatch"


def test_current_v2_workflow_remains_source_of_package_recovery_fixture():
    source = audit._extract_v2_python()
    assert 'def prepare(' in source
    assert 'scenarios._HANDLERS["mixed_authority_recovery"]' in source
    assert "core_package_recovery" in audit.CUSTOM_V2_WRAPPER_IDS
    assert "core_package_recovery" in audit._internal_v2_wrapper_names()


def test_inventory_payload_is_json_serializable_and_core_only():
    payload = audit.inventory_payload(audit.scenario_inventory())
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "core_clinic_info" in encoded
    assert "CORE-TWO-SERVICES-SAME-VISIT" in encoded
    assert "core_package_recovery" in encoded
    assert "RA-G7-CATEGORY-SPLIT" not in encoded
    assert "mixed_authority_confirm_b" not in encoded


def test_execution_invariant_blocks_silent_handler_skip():
    specs = audit.scenario_inventory()
    complete = [
        {"scenario_id": spec.scenario_id, "handler_invoked": True}
        for spec in specs
    ]
    assert audit._execution_invariant_errors(specs, complete) == []

    complete[3]["handler_invoked"] = False
    errors = audit._execution_invariant_errors(specs, complete)
    assert len(errors) == 1
    assert "selected handlers never invoked" in errors[0]
    assert specs[3].scenario_id in errors[0]


def test_quality_flags_are_factual_only():
    flags = audit._quality_flags("hello\n\nhello")
    assert flags["duplicate_identical_response_sections"] is True
    assert flags["empty_reply"] is False
    assert flags["internal_marker_leakage"] is False


def test_targeted_repair_inventory_is_exact_five_conversations_nine_turns(monkeypatch):
    monkeypatch.setenv(
        audit.AUDIT_SELECTION_ENV,
        ",".join(audit.TARGETED_REPAIR_ORDER),
    )

    specs = audit.scenario_inventory()
    payload = audit.inventory_payload(specs)

    assert [item.scenario_id for item in specs] == list(audit.TARGETED_REPAIR_ORDER)
    assert payload["unique_conversations_selected"] == 5
    assert payload["turns_planned"] == 9
    assert payload["portfolio_mode"] == "targeted"
    assert payload["selector_invariant"] is True
    assert payload["selected_handler_ids"] == list(audit.TARGETED_REPAIR_ORDER)
    assert payload["executable_handler_ids"] == list(audit.TARGETED_REPAIR_ORDER)


def test_internal_v2_execution_is_scoped_by_targeted_selector():
    source = Path(audit.SCRIPT_PATH).read_text(encoding="utf-8")
    assert "selected_ids = set(_requested_scenario_ids())" in source
    assert "if scenario_id not in selected_ids:" in source
    assert "continue" in source[source.index("if scenario_id not in selected_ids:"):][:120]


def test_single_next_availability_inventory_is_one_conversation_two_turns(monkeypatch):
    monkeypatch.setenv(
        audit.AUDIT_SELECTION_ENV,
        "core_nearest_availability_continuation",
    )

    specs = audit.scenario_inventory()
    payload = audit.inventory_payload(specs)

    assert [item.scenario_id for item in specs] == [
        "core_nearest_availability_continuation"
    ]
    assert payload["unique_conversations_selected"] == 1
    assert payload["turns_planned"] == 2
    assert payload["portfolio_mode"] == "targeted"
    assert payload["selector_invariant"] is True
    assert payload["selected_handler_ids"] == [
        "core_nearest_availability_continuation"
    ]
    assert payload["executable_handler_ids"] == [
        "core_nearest_availability_continuation"
    ]
