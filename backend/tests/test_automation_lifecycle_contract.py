from pathlib import Path


def _source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "app/services/automations.py").read_text(encoding="utf-8")


def test_manual_cancellations_stay_terminal_but_lifecycle_cancellations_can_replan() -> None:
    source = _source()
    assert "REPLANNABLE_CANCELLATION_REASONS" in source
    planner = source.split("def plan_automation_jobs(", 1)[1].split("def claim_due_jobs(", 1)[0]
    assert "renewable_cancel = _cancelled_job_can_be_replanned(existing)" in planner
    assert 'existing.status in {"queued", "failed"} or renewable_cancel' in planner
    assert "Manual job cancellations stay terminal" in planner


def test_planner_cancels_only_safely_queued_stale_deliveries() -> None:
    source = _source()
    assert "def _cancel_pending_job_dispatch(" in source
    assert 'dispatch.status != "queued"' in source
    assert 'message.delivery_status == "queued"' in source
    planner = source.split("def plan_automation_jobs(", 1)[1].split("def claim_due_jobs(", 1)[0]
    assert 'AutomationJob.status.in_(("queued", "failed", "dispatched"))' in planner
    assert "Appointment or rule became ineligible before provider send." in planner


def test_reschedule_requeues_only_before_provider_send() -> None:
    source = _source()
    planner = source.split("def plan_automation_jobs(", 1)[1].split("def claim_due_jobs(", 1)[0]
    assert 'existing.status == "dispatched" and existing.scheduled_for != when' in planner
    assert '"reason": "rescheduled_before_provider_send"' in planner
    assert "existing.message_id = None" in planner
    assert "existing.dispatch_id = None" in planner


def test_planner_batches_dedupe_and_projects_idle_state_before_full_rows() -> None:
    source = _source()
    planner = source.split("def plan_automation_jobs(", 1)[1].split("def claim_due_jobs(", 1)[0]
    assert "AutomationJob.dedupe_key.in_(dedupe_keys)" in planner
    assert "existing_state_by_key" in planner
    assert "mutation_ids" in planner
    assert "Stable jobs need no full-row read" in planner
    assert "Appointment.status.label(\"appointment_status\")" in planner
    assert "stale_job_ids" in planner
