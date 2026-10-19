from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE3_AUDIT = REPO_ROOT / "backend" / "scripts" / "run_phase3_real_world_agent_audit.py"
GROUPED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "grouped-multiservice-intent-live-review.yml"
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


def _workflow_step(source: str, name: str, next_name: str) -> str:
    start = source.index(f"      - name: {name}")
    end = source.index(f"      - name: {next_name}", start)
    return source[start:end]


def test_phase3_is_manual_opt_in_while_required_grouped_gates_remain_automatic() -> None:
    source = GROUPED_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:\n    inputs:\n      run_phase3:" in source
    assert "type: boolean" in source
    assert "default: false" in source

    full_grouped = _workflow_step(
        source,
        "Run full grouped multi-service review",
        "Run Phase 3.3 real-world agent strength audit",
    )
    assert "\n        if:" not in full_grouped

    phase3 = _workflow_step(
        source,
        "Run Phase 3.3 real-world agent strength audit",
        "Upload Phase 3.3 real-world audit diagnostics",
    )
    assert "github.event_name == 'workflow_dispatch'" in phase3
    assert "inputs.run_phase3" in phase3
    assert "always()" in phase3
    assert "!cancelled()" in phase3

    phase3_upload = _workflow_step(
        source,
        "Upload Phase 3.3 real-world audit diagnostics",
        "Upload RA-G standalone diagnostics",
    )
    assert "github.event_name == 'workflow_dispatch'" in phase3_upload
    assert "inputs.run_phase3" in phase3_upload

    assert "batch1-compatibility:" in source
    assert "Run RA-G1 through RA-G5 on audit-only #241 + #249 overlay" in source


def test_phase3_manual_diagnostic_implementation_remains_available() -> None:
    source = GROUPED_WORKFLOW.read_text(encoding="utf-8")
    assert "backend/scripts/run_phase3_real_world_agent_audit.py" in source
    assert "python scripts/run_phase3_real_world_agent_audit.py" in source
    assert "phase3-real-world-agent-audit.log" in source


def test_c7_is_out_of_required_automatic_grouped_path() -> None:
    workflow = GROUPED_WORKFLOW.read_text(encoding="utf-8")
    grouped_runner = (
        REPO_ROOT / "backend" / "scripts" / "run_grouped_multiservice_intent_live_review.py"
    ).read_text(encoding="utf-8")

    assert "Run focused C7 replacement" not in workflow
    assert "Run focused C7 pure addition" not in workflow
    assert "Run full grouped multi-service review" in workflow
    assert "Run RA-G1 through RA-G5 on audit-only #241 + #249 overlay" in workflow
    assert 'if not selected and name.startswith("RA-C7-"):' in grouped_runner
