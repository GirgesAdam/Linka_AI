from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.services.analytics import ANALYTICS_ALLOWED_DAYS, _percent, analytics_period_bounds


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_analytics_periods_are_bounded_and_calendar_aligned() -> None:
    assert ANALYTICS_ALLOWED_DAYS == (7, 30, 90)
    start_at, end_at, start_date, local_today = analytics_period_bounds(
        timezone_name="Africa/Cairo",
        days=7,
        now=datetime(2026, 8, 25, 16, 35, tzinfo=UTC),
    )
    assert start_date == date(2026, 8, 19)
    assert local_today == date(2026, 8, 25)
    assert start_at == datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
    assert end_at == datetime(2026, 8, 25, 21, 0, tzinfo=UTC)


def test_analytics_rejects_unbounded_periods() -> None:
    with pytest.raises(ValueError, match="days must be one of"):
        analytics_period_bounds(timezone_name="Africa/Cairo", days=365)


def test_rates_are_deterministic_and_zero_safe() -> None:
    assert _percent(8, 10) == 80.0
    assert _percent(1, 3) == 33.3
    assert _percent(0, 0) == 0.0


def test_analytics_route_is_read_only_workspace_scoped_and_period_limited() -> None:
    root = _root()
    route = (root / "backend/app/api/routes/analytics.py").read_text(encoding="utf-8")
    router = (root / "backend/app/api/router.py").read_text(encoding="utf-8")
    assert '@router.get("/overview"' in route
    assert 'get_analytics_section_reader("administration")' in route
    assert "days: int = 30" in route
    assert "Literal[7, 30, 90]" not in route
    assert "days not in ANALYTICS_ALLOWED_DAYS" in route
    assert "HTTPException(status_code=422" in route
    assert "get_workspace_admin" not in route
    assert 'prefix="/analytics"' in router
    assert '@router.post("/ask"' not in route
    assert '@router.post("/compose"' not in route
    assert '.commit(' not in route and '.add(' not in route and '.delete(' not in route



def test_analytics_query_period_contract_uses_integer_coercion() -> None:
    route = (_root() / "backend/app/api/routes/analytics.py").read_text(encoding="utf-8")
    # HTTP query parameters arrive as strings. FastAPI must first coerce them to int,
    # then our deterministic allow-list validates 7/30/90. Literal[int] under
    # Pydantic v2 rejects the raw string before coercion.
    assert "days: int = 30" in route
    assert "ANALYTICS_ALLOWED_DAYS" in route
    assert "Literal[7, 30, 90]" not in route

def test_analytics_service_uses_canonical_data_and_never_calls_ai() -> None:
    source = (_root() / "backend/app/services/analytics.py").read_text(encoding="utf-8").lower()
    for model in ("appointment", "patient", "conversation", "handoffrequest", "service", "branch"):
        assert model.lower() in source
    assert "generative" not in source
    assert "gemini" not in source
    assert "llm" not in source
    assert "re.compile" not in source
    assert "re.search" not in source


def test_analytics_excludes_reschedule_shells_and_refunds_from_recorded_payments() -> None:
    source = (_root() / "backend/app/services/analytics.py").read_text(encoding="utf-8")
    assert 'Appointment.status != "rescheduled"' in source
    assert 'Appointment.status == "completed"' in source
    assert 'PaymentTransaction.transaction_type == "payment"' in source
    assert 'PaymentTransaction.transaction_type == "refund"' in source
    assert '.group_by(Appointment.currency)' in source
    assert "recorded_paid_minor" in source
    assert "outstanding_balance_minor" in source


def test_daily_buckets_use_workspace_timezone_and_zero_fill_calendar_days() -> None:
    source = (_root() / "backend/app/services/analytics.py").read_text(encoding="utf-8")
    assert "func.timezone(resolved_timezone_name, Appointment.start_at)" in source
    assert "func.timezone(resolved_timezone_name, Patient.created_at)" in source
    assert "while current_day <= local_today" in source
    assert "patient_daily.get(current_day, 0)" in source


def test_analytics_ui_has_catalog_period_filters_and_no_chart_dependency() -> None:
    root = _root()
    page = (root / "frontend/src/app/(dashboard)/analytics/page.tsx").read_text(encoding="utf-8")
    catalog = (root / "frontend/src/app/(dashboard)/analytics/catalog.tsx").read_text(encoding="utf-8")
    navigation = (root / "frontend/src/components/dashboard-navigation.tsx").read_text(encoding="utf-8")
    types = (root / "frontend/src/lib/types.ts").read_text(encoding="utf-8")

    assert 'tiaRequest<AnalyticsCatalog>("/analytics/catalog")' in page
    assert "<AnalyticsCatalogPanel" in page
    assert 'name="period"' in catalog
    assert '<option value="7">' in catalog
    assert '<option value="30">' in catalog
    assert '<option value="90">' in catalog
    assert 'name="currency"' not in catalog
    assert "recharts" not in page.lower()
    assert "recharts" not in catalog.lower()
    assert 'href: "/analytics"' in navigation
    assert "icon: BarChart3" in navigation
    assert "export interface AnalyticsCatalog" in types
