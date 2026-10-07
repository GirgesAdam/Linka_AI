from pathlib import Path

from app.services.automations import (
    AUTOMATION_JOB_STALE_MINUTES,
    AUTOMATION_WORKER_FRESH_MINUTES,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_automation_ops_uses_minute_scheduler_health_thresholds() -> None:
    assert 2 <= AUTOMATION_WORKER_FRESH_MINUTES <= 5
    assert AUTOMATION_JOB_STALE_MINUTES >= 10


def test_automation_overview_exposes_queue_delivery_and_worker_health() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    schema = (_root() / "backend/app/schemas/automation.py").read_text(encoding="utf-8")

    assert "def automation_operations_overview(" in service
    assert "delivery_failed_jobs" in service
    assert "attention_count" in service
    assert "worker_state" in service
    assert "class AutomationOperationsOverview" in schema


def test_manual_retry_preserves_idempotency_and_blocks_provider_duplicates() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")

    retry = service.split("def retry_automation_job(", 1)[1].split("def cancel_automation_job(", 1)[0]
    assert ".with_for_update()" in retry
    assert 'job.status == "failed"' in retry
    assert 'dispatch.status != "failed"' in retry
    assert "dispatch.provider_message_id" in retry
    assert "avoid duplicates" in retry
    assert 'dispatch.status = "queued"' in retry
    assert "dispatch.attempts = 0" in retry
    assert "MessageDispatch(" not in retry
    assert "Message(" not in retry


def test_manual_cancel_only_cancels_unsent_work() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")

    cancel = service.split("def cancel_automation_job(", 1)[1].split("def execute_job(", 1)[0]
    assert ".with_for_update()" in cancel
    assert 'job.status in {"queued", "failed"}' in cancel
    assert 'dispatch.status != "queued"' in cancel
    assert 'dispatch.status = "cancelled"' in cancel
    assert 'message.delivery_status = "cancelled"' in cancel


def test_automation_job_list_surfaces_transport_failures_without_n_plus_one() -> None:
    route = (_root() / "backend/app/api/routes/automations.py").read_text(encoding="utf-8")

    jobs = route.split('@router.get("/jobs"', 1)[1].split('@router.post("/jobs/{job_id}/retry"', 1)[0]
    assert "outerjoin(MessageDispatch" in jobs
    assert "dispatch_last_error" in jobs
    assert 'attention_reason = "delivery_failed"' in jobs
    assert "db.get(MessageDispatch" not in jobs


def test_retry_and_cancel_routes_require_workspace_admin() -> None:
    route = (_root() / "backend/app/api/routes/automations.py").read_text(encoding="utf-8")

    retry = route.split('@router.post("/jobs/{job_id}/retry"', 1)[1].split('@router.post("/jobs/{job_id}/cancel"', 1)[0]
    cancel = route.split('@router.post("/jobs/{job_id}/cancel"', 1)[1].split('@router.post("/workers"', 1)[0]
    assert "get_workspace_admin" in retry
    assert "get_workspace_admin" in cancel


def test_automation_dashboard_has_health_attention_safe_actions_and_product_whitelist() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/automations/page.tsx").read_text(encoding="utf-8")
    actions = (_root() / "frontend/src/app/(dashboard)/automations/actions.ts").read_text(encoding="utf-8")
    timing = (_root() / "frontend/src/components/automation-timing-form.tsx").read_text(encoding="utf-8")

    assert 'tiaRequest<AutomationOperationsOverview>("/automations/overview")' in page
    assert "attentionLabel" in page
    assert "attentionJobs" in page
    assert "dispatch_status" in page
    assert "retryAutomationJob" in page
    assert "cancelAutomationJob" in page
    assert "visibleProductRuleKeys" in page
    assert '"cancellation_recovery"' in page
    assert '"no_show_followup"' not in page
    assert "AutomationTimingForm" in page
    assert "saveAutomationTiming" in timing
    assert '/automations/jobs/${id}/retry' in actions
    assert '/automations/jobs/${id}/cancel' in actions


def test_booking_confirmation_is_retired_from_automation_product() -> None:
    core = (_root() / "backend/app/core/automation_rules.py").read_text(encoding="utf-8")
    route = (_root() / "backend/app/api/routes/automations.py").read_text(encoding="utf-8")
    scheduler = (_root() / "backend/app/runtime/automation_scheduler.py").read_text(encoding="utf-8")
    setup = (_root() / "n8n/AUTOMATIONS_SETUP.md").read_text(encoding="utf-8")

    assert 'key="booking_confirmation"' not in core
    assert 'if rule.key != "booking_confirmation"' in route
    assert 'if rule is None or rule.key == "booking_confirmation"' in route
    assert 'AutomationRule.key == "booking_confirmation"' in scheduler
    assert "legacy_rule.enabled = False" in scheduler
    assert "There is no separate automatic booking-confirmation rule." in setup


def test_operational_readiness_tracks_current_migration_head() -> None:
    readiness = (_root() / "backend/app/services/operational_readiness.py").read_text(encoding="utf-8")
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness


def test_reminder_and_post_visit_fallback_copy_match_current_template_contract() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    setup = (_root() / "n8n/AUTOMATIONS_SETUP.md").read_text(encoding="utf-8")

    reminder = service.split('if rule_key == "appointment_reminder_6h":', 1)[1].split('if rule_key == "appointment_reminder_24h":', 1)[0]
    post = service.split('if rule_key == "post_visit_followup":', 1)[1].split('if rule_key == "no_show_followup":', 1)[0]
    assert "تأكد حضورك" in reminder
    assert "فاضل حوالي 6 ساعات" not in reminder
    assert "{data['date']}" in reminder
    assert "{data['branch_name']}" not in reminder
    assert "تغيير الميعاد" in reminder
    assert "حبيت أطمن عليكي بعد {data['service_name']}" in post
    assert "كل حاجة تمام؟" in post
    assert "جلسة {{2}}" in setup
    assert "بعد {{2}}" in setup


def test_fixed_six_hour_meta_template_blocks_incompatible_admin_timing() -> None:
    route = (_root() / "backend/app/api/routes/automations.py").read_text(encoding="utf-8")
    assert 'rule.template_name == "tia_reminder_6h_01"' in route
    assert 'changes["offset_minutes"] != -360' in route
    assert 'Switch to the timing-neutral reminder template' in route
