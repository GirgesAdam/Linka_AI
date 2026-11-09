from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import app.services.analytics_catalog as catalog_service
from app.core.config import settings
from app.schemas.analytics_catalog import AnalyticsCatalogRunRequest
from app.services.analytics_capacity import is_statement_timeout
from app.services.analytics_export import AnalyticsExportLimitError, analytics_result_csv
from app.services.analytics_runtime import clear_analytics_aggregate_cache
from tests.test_deterministic_analytics_catalog_phase72 import NOW, _engine, _seed


def test_aggregate_micro_cache_skips_duplicate_heavy_execution_but_tracks_as_of_bucket(monkeypatch) -> None:
    engine = _engine()
    clear_analytics_aggregate_cache()
    monkeypatch.setattr(settings, "analytics_aggregate_cache_ttl_seconds", 5)
    monkeypatch.setattr(settings, "analytics_aggregate_cache_max_entries", 32)

    calls = 0
    original = catalog_service.execute_business_plan

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog_service, "execute_business_plan", counted)
    with Session(engine) as db:
        ids = _seed(db)
        request = AnalyticsCatalogRunRequest(analysis_key="revenue_overview", lookback_days=30)
        first = catalog_service.run_catalog_analysis(
            db, workspace_id=ids["workspace"], request=request, now=NOW
        )
        second = catalog_service.run_catalog_analysis(
            db, workspace_id=ids["workspace"], request=request, now=NOW
        )
        assert first == second
        assert calls == 1

        # Relative periods are keyed by a bounded as-of bucket, so a request at
        # a materially different time cannot reuse the older relative window.
        catalog_service.run_catalog_analysis(
            db,
            workspace_id=ids["workspace"],
            request=request,
            now=NOW + timedelta(seconds=10),
        )
        assert calls == 2
    clear_analytics_aggregate_cache()


def test_patient_lists_never_use_aggregate_cache(monkeypatch) -> None:
    engine = _engine()
    clear_analytics_aggregate_cache()
    monkeypatch.setattr(settings, "analytics_aggregate_cache_ttl_seconds", 30)
    monkeypatch.setattr(settings, "analytics_aggregate_cache_max_entries", 32)

    calls = 0
    original = catalog_service.execute_audience_plan

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog_service, "execute_audience_plan", counted)
    with Session(engine) as db:
        ids = _seed(db)
        request = AnalyticsCatalogRunRequest(
            analysis_key="lapsed_patients", inactivity_days=180, limit=25
        )
        catalog_service.run_catalog_analysis(
            db, workspace_id=ids["workspace"], request=request, now=NOW
        )
        catalog_service.run_catalog_analysis(
            db, workspace_id=ids["workspace"], request=request, now=NOW
        )
        assert calls == 2
    clear_analytics_aggregate_cache()


def test_export_limits_are_server_side_and_bounded(monkeypatch) -> None:
    engine = _engine()
    with Session(engine) as db:
        ids = _seed(db)
        result = catalog_service.run_catalog_analysis(
            db,
            workspace_id=ids["workspace"],
            request=AnalyticsCatalogRunRequest(
                analysis_key="revenue_by_service", lookback_days=30
            ),
            now=NOW,
            use_cache=False,
        )
        assert len(result.rows) == 2
        monkeypatch.setattr(settings, "analytics_export_max_rows", 1)
        with pytest.raises(AnalyticsExportLimitError, match="محدود"):
            analytics_result_csv(result)


def test_postgres_statement_timeout_detection_uses_query_canceled_sqlstate() -> None:
    class Original:
        sqlstate = "57014"

    class FakeError:
        orig = Original()

    assert is_statement_timeout(FakeError()) is True  # type: ignore[arg-type]


def test_phase75_uses_isolated_pool_timeout_cache_export_guards_and_payment_index() -> None:
    root = Path(__file__).resolve().parents[1]
    session = (root / "app/database/session.py").read_text(encoding="utf-8")
    dependency = (root / "app/api/dependencies/analytics.py").read_text(encoding="utf-8")
    route = (root / "app/api/routes/analytics.py").read_text(encoding="utf-8")
    runtime = (root / "app/services/analytics_runtime.py").read_text(encoding="utf-8")
    export = (root / "app/services/analytics_export.py").read_text(encoding="utf-8")
    model = (root / "app/models/payment_transaction.py").read_text(encoding="utf-8")
    migration = (root / "alembic/versions/0043_analytics_scale_guards.py").read_text(encoding="utf-8")
    readiness = (root / "app/services/operational_readiness.py").read_text(encoding="utf-8")

    assert "analytics_engine = create_engine" in session
    assert "pool_size=settings.analytics_db_pool_size" in session
    assert "max_overflow=settings.analytics_db_max_overflow" in session
    assert "SET LOCAL statement_timeout" in session
    assert "SET LOCAL application_name = 'tia-analytics'" in session
    assert "AnalyticsSessionLocal" in dependency
    assert "with SessionLocal() as auth_db" in dependency
    assert "get_analytics_page_reader" in dependency
    assert "HTTP_503_SERVICE_UNAVAILABLE" in dependency
    assert "get_analytics_db" in route
    assert 'get_analytics_page_reader("analytics.overview", legacy_access="reader")' in route
    assert "get_workspace_reader" not in route
    assert "Depends(get_db)" not in route
    assert "use_cache=False" in route
    assert "result.result_kind == \"patient_list\"" in runtime
    assert "analytics_export_max_rows" in export
    assert "analytics_export_max_bytes" in export
    assert "ix_payment_transactions_workspace_currency_created" in model
    assert 'revision: str = "0043_analytics_scale_guards"' in migration
    assert 'down_revision: str | None = "0042_analytics_saved_views"' in migration
    assert "autocommit_block" in migration
    assert "postgresql_concurrently=True" in migration
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness


def test_catalog_validation_reuses_entity_catalog_in_same_run(monkeypatch) -> None:
    engine = _engine()
    clear_analytics_aggregate_cache()
    calls = 0
    original = catalog_service.analytics_entity_catalog

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog_service, "analytics_entity_catalog", counted)
    with Session(engine) as db:
        ids = _seed(db)
        catalog_service.run_catalog_analysis(
            db,
            workspace_id=ids["workspace"],
            request=AnalyticsCatalogRunRequest(
                analysis_key="revenue_overview", lookback_days=30
            ),
            now=NOW,
            use_cache=False,
        )
        assert calls == 1
