from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE3_AUDIT = REPO_ROOT / "backend" / "scripts" / "run_phase3_real_world_agent_audit.py"
SPOKEN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "clinic-hours-clock-real-agent-review.yml"
RETIRED_SPOKEN_FILES = (
    REPO_ROOT / "backend" / "scripts" / "run_clinic_hours_clock_live_review.py",
    REPO_ROOT / "backend" / "scripts" / "spoken_clock_live_review_base.py",
    REPO_ROOT / "backend" / "tests" / "test_spoken_clock_live_review_counters.py",
)


def _scenario_names() -> list[str]:
    tree = ast.parse(PHASE3_AUDIT.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "SCENARIOS" for target in node.targets):
            continue
        assert isinstance(node.value, ast.Tuple)
        names: list[str] = []
        for element in node.value.elts:
            assert isinstance(element, ast.Call)
            assert element.args and isinstance(element.args[0], ast.Constant)
            names.append(str(element.args[0].value))
        return names
    raise AssertionError("SCENARIOS registry not found")


def test_spoken_clock_live_review_is_fully_retired() -> None:
    assert not SPOKEN_WORKFLOW.exists()
    assert all(not path.exists() for path in RETIRED_SPOKEN_FILES)


def test_phase3_contract_contains_ten_scenarios_without_rw7() -> None:
    names = _scenario_names()
    assert names == [
        "RW-1",
        "RW-2",
        "RW-3",
        "RW-4",
        "RW-5",
        "RW-6",
        "RW-8",
        "RW-9",
        "RW-10",
        "RW-11",
    ]
    source = PHASE3_AUDIT.read_text(encoding="utf-8")
    assert "def _validate_rw7" not in source
    assert "RW7 =" not in source
    assert 'ScenarioSpec("RW-7"' not in source


def test_retired_gates_do_not_remove_deterministic_business_coverage() -> None:
    spoken_policy = (REPO_ROOT / "backend" / "tests" / "test_v2_spoken_clock_policy.py").read_text(encoding="utf-8")
    active_task = (REPO_ROOT / "backend" / "tests" / "test_v2_active_task_bare_clock_boundary.py").read_text(encoding="utf-8")
    grouped = (REPO_ROOT / "backend" / "tests" / "test_v2_grouped_multiservice_intent_reliability.py").read_text(encoding="utf-8")
    responder = (REPO_ROOT / "backend" / "tests" / "test_v2_responder.py").read_text(encoding="utf-8")

    assert '"10:30", "am", "10:30"' in active_task
    assert '"10:30", "pm", "22:30"' in active_task
    assert "test_bare_12_remains_noon" in spoken_policy
    assert "test_explicit_midnight_12_uses_existing_special_case" in spoken_policy
    assert "test_rw7_redundant_full_group_time_only_preserves_durable_date" in grouped
    assert "test_rw7_explicit_date_change_still_replaces_durable_date" in grouped
    assert "test_rw7_redundancy_normalization_does_not_hide_real_service_addition" in grouped
    assert "test_rw7_compound_booking_proposal_renders_initial_and_corrected_visit_times" in responder
