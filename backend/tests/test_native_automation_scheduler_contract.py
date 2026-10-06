from pathlib import Path


def _source() -> str:
    root = Path(__file__).resolve().parent.parent
    return (root / "app/runtime/automation_scheduler.py").read_text(encoding="utf-8")


def test_native_scheduler_reuses_tia_automation_services() -> None:
    source = _source()
    assert "plan_automation_jobs(" in source
    assert "claim_due_jobs(" in source
    assert "execute_job(" in source
    assert "run_scheduled_sync_tick(" in source


def test_native_scheduler_is_not_an_http_or_n8n_bridge() -> None:
    source = _source().lower()
    assert "requests." not in source
    assert "httpx" not in source
    assert "urllib" not in source
    assert "n8n" not in source


def test_native_scheduler_processes_only_active_workspaces_and_heartbeats() -> None:
    source = _source()
    assert "Workspace.is_active.is_(True)" in source
    assert "RUNTIME_WORKER_NAME" in source
    assert "last_seen_at" in source


def test_native_scheduler_uses_conservative_planning_preflight_and_reuses_rules() -> None:
    source = _source()
    assert "automation_planning_may_have_work(" in source
    assert "planning_skipped_preflight" in source
    assert "rules = ensure_default_rules(db, workspace.id)" in source
    assert "rules=rules" in source
    assert "PlanningResult(planned=0, cancelled=0)" in source
