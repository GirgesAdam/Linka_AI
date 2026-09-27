from tools.agent_eval import registry
from tools.agent_eval import run_batch_08_10_combined as combined


def test_combined_batch_registry_has_25_stable_scenarios():
    rows = registry.scenarios_for_batch("combined_08_10")
    assert len(rows) == 25
    assert [row.id for row in rows] == [f"S{i}" for i in range(1, 26)]
    assert rows[0].canonical_id == "c0810_01_service_catalog_discovery"
    assert rows[-1].canonical_id == "c0810_25_long_detour_resume_appointment"


def test_combined_batch_runners_resolve_and_match_cases():
    rows = registry.scenarios_for_batch("combined_08_10")
    assert [row.resolve_runner() for row in rows] == combined.CASES


def test_combined_batch_has_required_safety_counters():
    required = {
        "wrong_active_task_target",
        "unexpected_task_restart",
        "unexpected_task_loss",
        "duplicate_writes",
        "stale_lifecycle_writes",
        "wrong_appointment_writes",
        "side_read_business_writes",
        "financial_boundary_violations",
        "human_ownership_writes",
        "invented_entity_writes",
    }
    assert required <= set(combined.STALE_COUNTER_KEYS)


def test_combined_batch_selector_accepts_id_and_canonical_id():
    by_id = registry.select_scenarios("combined_08_10", selectors={"S24"})
    assert [row.canonical_id for row in by_id] == ["c0810_24_financial_boundary_read"]

    by_canonical = registry.select_scenarios(
        "combined_08_10",
        selectors={"c0810_03_doctors_for_service"},
    )
    assert [row.id for row in by_canonical] == ["S3"]
