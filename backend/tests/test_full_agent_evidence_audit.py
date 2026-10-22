from __future__ import annotations

import json

import scripts.run_full_agent_evidence_audit as audit


def test_full_audit_inventory_is_unique_and_excludes_out_of_scope_scenarios():
    specs = audit.scenario_inventory()
    payload = audit.inventory_payload(specs)

    assert payload["unique_conversations_selected"] == 17
    assert payload["turns_planned"] == 46
    assert payload["duplicates_intentionally_executed"] == 0
    assert payload["c7"] == "NOT_INCLUDED"
    assert payload["phase_3_3"] == "NOT_RUN"
    assert len({item.scenario_id for item in specs}) == 17
    assert not any("C7" in item.scenario_id for item in specs)


def test_assert_recorder_captures_behavioral_mismatches_without_stopping_execution():
    source = "x = 1\nassert x == 2, 'legacy mismatch'\ny = 3\nassert y == 3\n"
    namespace = {"_audit_eval_check": audit._audit_eval_check}
    audit._AUDIT_CHECKS.clear()

    exec(audit._compile_with_recorded_asserts(source, source_name="unit.py"), namespace)

    assert namespace["y"] == 3
    assert [row["condition_result"] for row in audit._AUDIT_CHECKS] == [False, True]
    assert audit._AUDIT_CHECKS[0]["legacy_error_if_false"] == "legacy mismatch"


def test_existing_v2_workflow_is_the_source_of_audit_scenarios():
    source = audit._extract_v2_python()
    counts = audit._v2_turn_counts()

    assert 'scenarios._HANDLERS["mixed_authority_recovery"]' in source
    assert 'scenarios._HANDLERS["mixed_authority_confirm_b"]' in source
    assert counts == {"mixed_authority_recovery": 5, "mixed_authority_confirm_b": 2}


def test_inventory_payload_is_json_serializable():
    payload = audit.inventory_payload(audit.scenario_inventory())
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "RA-H1" in encoded
    assert "RA-G7-CATEGORY-SPLIT" in encoded
    assert "mixed_authority_recovery" in encoded


def test_quality_flags_are_factual_only():
    flags = audit._quality_flags("hello\n\nhello")
    assert flags["duplicate_identical_response_sections"] is True
    assert flags["empty_reply"] is False
    assert flags["internal_marker_leakage"] is False
