from pathlib import Path

from app.schemas.dashboard import DashboardSummaryRead


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_dashboard_summary_exposes_explicit_today_and_after_today_partitions() -> None:
    fields = DashboardSummaryRead.model_fields

    assert "appointments_after_today" in fields
    assert "today_appointments" in fields
    assert "next_appointments" in fields


def test_dashboard_backend_owns_day_partition_semantics() -> None:
    route = (_root() / "backend/app/api/routes/dashboard.py").read_text(encoding="utf-8")

    assert "Appointment.start_at >= start_utc" in route
    assert "Appointment.start_at <= end_utc" in route
    assert "Appointment.start_at > end_utc" in route
    assert 'Appointment.status.in_(("pending", "confirmed", "checked_in", "in_progress"))' in route
    assert 'Appointment.status.in_(("pending", "confirmed"))' in route
    assert "appointments_after_today=appointments_after_today" in route
    assert "today_appointments=appointment_reads(today_rows)" in route
    assert "next_appointments=appointment_reads(next_rows)" in route


def test_dashboard_ui_prioritizes_attention_today_and_next_without_financial_inference() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/dashboard/page.tsx").read_text(encoding="utf-8")
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")

    attention = workspace.index('id="needs-attention-heading"')
    today = workspace.index('id="today-heading"')
    coming_next = workspace.index('id="next-heading"')
    overview = workspace.index('id="overview-heading"')

    assert attention < today < coming_next < overview
    assert 'tiaRequest<CRMTask[]>("/crm/tasks?scope=overdue&limit=5")' in page
    assert 'tiaRequest<HandoffQueueItem[]>("/inbox/handoffs?limit=5")' in page
    assert "summary.failed_automation_jobs" in workspace
    assert "setup.readiness.missing" in workspace
    assert "summary.today_appointments" in workspace
    assert "summary.next_appointments" in workspace
    assert "summary.upcoming_appointments" in workspace
    assert 'href={`/appointments/${summary.next_appointments[0].id}`}' in workspace
    assert '<StatusBadge domain="appointment"' in workspace
    assert '<StatusBadge domain="priority"' in workspace
    assert "formatMoney" not in workspace
    assert "price_minor" not in workspace
    assert "StatCard" not in workspace


def test_dashboard_partial_failures_stay_local() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/dashboard/page.tsx").read_text(encoding="utf-8")
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")

    assert "Promise.allSettled" in page
    assert 'if (summaryResult.status === "rejected") throw summaryResult.reason' in page
    assert "handoffsUnavailable" in page and "handoffsUnavailable" in workspace
    assert "setupUnavailable" in page and "setupUnavailable" in workspace
    assert "overdueTasksUnavailable" in page and "overdueTasksUnavailable" in workspace
    assert "LocalFailure" in workspace


def test_dashboard_uses_linka_interaction_tokens_not_legacy_teal() -> None:
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")

    assert "teal-" not in workspace
    assert "var(--interactive)" in workspace
