from datetime import UTC, datetime
from pathlib import Path

from app.api.routes.dashboard import _summarize_today_revenue, _today_bounds
from app.schemas.dashboard import DashboardSummaryRead, DashboardTodayRead


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_dashboard_keeps_legacy_summary_and_adds_focused_today_contract() -> None:
    assert "today_revenue" in DashboardSummaryRead.model_fields
    fields = DashboardTodayRead.model_fields
    assert set(fields) == {"timezone", "local_date", "appointments", "next_appointment_id"}


def test_today_bounds_respect_clinic_midnight() -> None:
    tz, now, start_utc, next_start_utc = _today_bounds(
        "Africa/Cairo",
        now=datetime(2026, 10, 4, 22, 30, tzinfo=UTC),
    )
    assert tz.key == "Africa/Cairo"
    assert now.astimezone(tz).date().isoformat() == "2026-10-05"
    assert start_utc == datetime(2026, 10, 4, 21, 0, tzinfo=UTC)
    assert next_start_utc == datetime(2026, 10, 5, 21, 0, tzinfo=UTC)


def test_today_appointments_are_backend_partitioned_by_clinic_day() -> None:
    route = (_root() / "backend/app/api/routes/dashboard.py").read_text(encoding="utf-8")
    assert '@router.get("/today", response_model=DashboardTodayRead)' in route
    assert "reference.astimezone(tz).date()" in route
    assert "Appointment.start_at >= start_utc" in route
    assert "Appointment.start_at < next_day_start_utc" in route
    assert '"completed"' in route
    assert '"cancelled"' in route
    assert '"no_show"' in route
    today_block = route.split('@router.get("/today", response_model=DashboardTodayRead)', 1)[1].split('@router.get("/today-revenue"', 1)[0]
    assert '"rescheduled"' not in today_block


def test_next_appointment_is_backend_owned_and_only_non_terminal() -> None:
    route = (_root() / "backend/app/api/routes/dashboard.py").read_text(encoding="utf-8")
    today_block = route.split('@router.get("/today", response_model=DashboardTodayRead)', 1)[1].split('@router.get("/today-revenue"', 1)[0]
    assert 'appointment.status in {"pending", "confirmed", "checked_in", "in_progress"}' in today_block
    assert "appointment.end_at >= now" in today_block
    assert "next_appointment_id=next_appointment_id" in today_block


def test_today_revenue_uses_canonical_payment_transactions_and_created_at() -> None:
    route = (_root() / "backend/app/api/routes/dashboard.py").read_text(encoding="utf-8")
    revenue_block = route.split('@router.get("/today-revenue", response_model=DashboardTodayRevenueRead)', 1)[1].split('@router.get("/summary"', 1)[0]
    assert "PaymentTransaction.workspace_id == workspace_id" in revenue_block
    assert "PaymentTransaction.created_at >= start_utc" in revenue_block
    assert "PaymentTransaction.created_at < next_day_start_utc" in revenue_block
    assert "BookingSettings.default_currency" in revenue_block
    assert "PaymentAllocation" not in revenue_block


def test_today_revenue_is_net_collected_and_breakdown_reconciles() -> None:
    result = _summarize_today_revenue(
        [
            ("cash", "EGP", "payment", 5000),
            ("visa", "EGP", "payment", 4250),
            ("instapay", "EGP", "payment", 3200),
            ("visa", "EGP", "refund", 250),
            ("wallet", "EGP", "payment", 100),
            ("cash", "USD", "payment", 99999),
        ],
        currency="EGP",
    )
    assert result.gross_collected_minor == 12550
    assert result.refunds_minor == 250
    assert result.total_minor == 12300
    assert result.cash_minor == 5000
    assert result.visa_minor == 4000
    assert result.instapay_minor == 3200
    assert result.other_minor == 100
    assert result.total_minor == result.cash_minor + result.visa_minor + result.instapay_minor + result.other_minor


def test_today_revenue_zero_is_valid() -> None:
    result = _summarize_today_revenue([], currency="EGP")
    assert result.total_minor == 0
    assert result.cash_minor == 0
    assert result.visa_minor == 0
    assert result.instapay_minor == 0


def test_home_shows_team_messages_and_due_followups_without_future_appointments() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/dashboard/page.tsx").read_text(encoding="utf-8")
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")
    assert 'tiaRequest<DashboardToday>("/dashboard/today")' in page
    assert 'tiaRequest<InboxConversationListItem[]>("/inbox/conversations?owner_type=human&status=pending&limit=100")' in page
    assert 'tiaRequest<CRMTask[]>("/crm/tasks?scope=due&task_type=follow_up&limit=100")' in page
    assert 'tiaRequest<DashboardTodayRevenue>("/dashboard/today-revenue")' in page
    assert "today.appointments" in workspace
    assert "رسائل على الفريق" in workspace
    assert "المتابعات المستحقة" in workspace
    assert "إيرادات اليوم" in workspace
    assert 'href={`/inbox/${conversation.id}`}' in workspace
    assert "summary.next_appointments" not in workspace
    assert "upcoming_appointments" not in workspace
    assert "active_patients" not in workspace
    assert "price_minor" not in workspace


def test_team_messages_and_followups_keep_old_unresolved_work_visible() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/dashboard/page.tsx").read_text(encoding="utf-8")
    crm_route = (_root() / "backend/app/api/routes/crm.py").read_text(encoding="utf-8")
    assert "owner_type=human&status=pending" in page
    assert "scope=due&task_type=follow_up" in page
    assert 'scope: Literal["all", "overdue", "today", "upcoming", "due"]' in crm_route
    assert 'elif scope == "due":' in crm_route
    assert "CRMTask.due_at < tomorrow_utc" in crm_route
    assert "CRMTask.status.in_(ACTIVE_TASK_STATUSES)" in crm_route


def test_dashboard_followup_can_be_completed_inline_and_revalidates_home() -> None:
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")
    actions = (_root() / "frontend/src/app/(dashboard)/tasks/actions.ts").read_text(encoding="utf-8")
    assert 'form action={setTaskStatus}' in workspace
    assert 'name="status" value="completed"' in workspace
    assert "تعليم المتابعة كمكتملة" in workspace
    assert 'revalidatePath("/dashboard")' in actions


def test_secondary_home_failures_are_local() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/dashboard/page.tsx").read_text(encoding="utf-8")
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")
    assert "Promise.allSettled" in page
    assert 'if (todayResult.status === "rejected") throw todayResult.reason' in page
    assert "teamMessagesUnavailable" in page and "teamMessagesUnavailable" in workspace
    assert "followUpsUnavailable" in page and "followUpsUnavailable" in workspace
    assert "revenueUnavailable" in page and "revenueUnavailable" in workspace


def test_agenda_clock_is_frontend_only_and_has_no_product_polling() -> None:
    agenda = (_root() / "frontend/src/app/(dashboard)/dashboard/today-agenda.tsx").read_text(encoding="utf-8")
    assert '"use client"' in agenda
    assert "setInterval" in agenda
    assert "60_000" in agenda
    assert "tiaRequest" not in agenda
    assert "fetch(" not in agenda
    assert "التالي" in agenda
    assert "الآن" in agenda


def test_due_follow_up_scope_is_clinic_local_active_only() -> None:
    crm_route = (_root() / "backend/app/api/routes/crm.py").read_text(encoding="utf-8")
    assert 'scope: Literal["all", "overdue", "today", "upcoming", "due"]' in crm_route
    assert "task_type: CRMTaskType | None = None" in crm_route
    assert "CRMTask.task_type == task_type" in crm_route
    assert "CRMTask.status.in_(ACTIVE_TASK_STATUSES)" in crm_route
    assert 'elif scope in {"today", "upcoming", "due"}:' in crm_route
    assert 'elif scope == "due":' in crm_route
    assert "CRMTask.due_at < tomorrow_utc" in crm_route


def test_home_uses_no_legacy_teal() -> None:
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")
    agenda = (_root() / "frontend/src/app/(dashboard)/dashboard/today-agenda.tsx").read_text(encoding="utf-8")
    assert "teal-" not in workspace
    assert "teal-" not in agenda
