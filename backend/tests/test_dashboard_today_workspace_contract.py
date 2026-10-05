from pathlib import Path

from app.api.routes.dashboard import _summarize_today_revenue
from app.schemas.dashboard import DashboardSummaryRead


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_dashboard_summary_keeps_compatibility_and_adds_today_revenue() -> None:
    fields = DashboardSummaryRead.model_fields
    assert "appointments_after_today" in fields
    assert "today_appointments" in fields
    assert "next_appointments" in fields
    assert "today_revenue" in fields
    assert "timezone" in fields


def test_dashboard_backend_owns_clinic_local_today_partition() -> None:
    route = (_root() / "backend/app/api/routes/dashboard.py").read_text(encoding="utf-8")
    assert "now.astimezone(tz).date()" in route
    assert "Appointment.start_at >= start_utc" in route
    assert "Appointment.start_at <= end_utc" in route
    assert "Appointment.start_at > end_utc" in route
    assert 'Appointment.status.in_(("pending", "confirmed", "checked_in", "in_progress", "completed"))' in route


def test_dashboard_revenue_uses_canonical_transactions_not_appointments() -> None:
    route = (_root() / "backend/app/api/routes/dashboard.py").read_text(encoding="utf-8")
    assert "PaymentTransaction.workspace_id == workspace_id" in route
    assert "PaymentTransaction.created_at >= start_utc" in route
    assert "PaymentTransaction.created_at < next_day_start_utc" in route
    assert 'transaction_type == "payment"' in route
    assert 'transaction_type == "refund"' in route
    assert 'net_by_method = {"cash": 0, "visa": 0, "instapay": 0, "other": 0}' in route
    assert "total_minor=gross_collected_minor - refunds_minor" in route
    assert "PaymentAllocation" not in route



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
    assert result.total_minor == (
        result.cash_minor + result.visa_minor + result.instapay_minor + result.other_minor
    )


def test_today_revenue_zero_is_valid_without_appointments() -> None:
    result = _summarize_today_revenue([], currency="EGP")
    assert result.total_minor == 0
    assert result.cash_minor == 0
    assert result.visa_minor == 0
    assert result.instapay_minor == 0


def test_dashboard_revenue_uses_workspace_currency_contract() -> None:
    route = (_root() / "backend/app/api/routes/dashboard.py").read_text(encoding="utf-8")
    assert "BookingSettings.default_currency" in route
    assert "str(row_currency).upper() != currency" in route


def test_dashboard_ui_is_today_only_and_has_no_financial_inference() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/dashboard/page.tsx").read_text(encoding="utf-8")
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")
    assert 'tiaRequest<DashboardSummary>("/dashboard/summary")' in page
    assert "summary.today_appointments" in workspace
    assert "summary.next_appointments" not in workspace
    assert "summary.upcoming_appointments" not in workspace
    assert "summary.appointments_after_today" not in workspace
    assert "price_minor" not in workspace
    assert "formatMoney(revenue.total_minor, revenue.currency)" in workspace
    assert "revenue.cash_minor" in workspace
    assert "revenue.visa_minor" in workspace
    assert "revenue.instapay_minor" in workspace
    assert "gross_collected_minor" not in workspace
    assert "PaymentTransaction" not in workspace


def test_dashboard_partial_failures_stay_local() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/dashboard/page.tsx").read_text(encoding="utf-8")
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")
    assert "Promise.allSettled" in page
    assert 'if (summaryResult.status === "rejected") throw summaryResult.reason' in page
    assert "handoffsUnavailable" in page and "handoffsUnavailable" in workspace
    assert "setupUnavailable" in page and "setupUnavailable" in workspace
    assert "overdueTasksUnavailable" in page and "overdueTasksUnavailable" in workspace


def test_dashboard_uses_linka_interaction_tokens_not_legacy_teal() -> None:
    workspace = (_root() / "frontend/src/app/(dashboard)/dashboard/dashboard-workspace.tsx").read_text(encoding="utf-8")
    assert "teal-" not in workspace
    assert "var(--interactive)" in workspace
