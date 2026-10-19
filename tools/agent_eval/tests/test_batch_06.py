import pytest

from tools.agent_eval.registry import scenarios_for_batch, select_scenarios
from tools.agent_eval.run import main

EXPECTED = [
    "b6_01_two_service_same_visit_success",
    "b6_02_second_component_unavailable",
    "b6_03_component_needs_device_clarification",
    "b6_04_shared_anchor_sequences_components",
    "b6_05_reschedule_entire_group",
    "b6_06_cancel_entire_standard_group",
    "b6_07_package_component_plus_standard_component",
    "b6_08_financial_handoff_blocks_grouped_write",
    "b6_09_buy_package_and_book_same_service",
    "b6_10_buy_package_a_book_a_and_b",
    "b6_11_replace_one_service_before_commit",
    "b6_12_change_device_for_one_component",
    "b6_13_side_price_query_preserves_compound",
    "b6_14_remove_one_component",
    "b6_15_sequence_crosses_resource_boundary",
    "b6_16_canonical_state_changes_before_compound_commit",
]


def test_batch6_registered_with_stable_scenarios() -> None:
    rows = scenarios_for_batch("batch_06")
    assert len(rows) == 16
    assert [row.id for row in rows] == [f"S{index}" for index in range(1, 17)]
    assert [row.canonical_id for row in rows] == EXPECTED


def test_batch6_quick_profile_selects_all(capsys) -> None:
    assert main(["--batch", "batch_06", "--profile", "quick"]) == 0
    output = capsys.readouterr().out
    assert "Selected scenarios: 16" in output
    assert "no DB or LLM execution" in output


def test_batch6_single_scenario_selector() -> None:
    rows = select_scenarios("batch_06", selectors={"S8"})
    assert [row.canonical_id for row in rows] == [EXPECTED[7]]


def test_batch6_unknown_selector_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown scenario selector"):
        select_scenarios("batch_06", selectors={"S99"})
