from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (_root() / relative).read_text(encoding="utf-8")


def test_reports_overview_prioritizes_decision_metrics_without_refund_card() -> None:
    overview = _read("frontend/src/app/(dashboard)/analytics/overview.tsx")

    assert 'label="المرتجعات"' not in overview
    assert "TrendingDown" not in overview
    for label in ("صافي الدخل", "الربح المسجل", "إجمالي المواعيد", "عملاء جدد"):
        assert f'label="{label}"' in overview
    assert "إجمالي المقبوضات" in overview
    assert "المصروفات المسجلة" in overview


def test_outstanding_balances_is_part_of_the_normal_report_catalog() -> None:
    page = _read("frontend/src/app/(dashboard)/analytics/page.tsx")
    catalog = _read("frontend/src/app/(dashboard)/analytics/catalog.tsx")

    assert 'href="/analytics/outstanding-balances"' not in page
    assert 'href="/analytics/outstanding-balances"' in catalog
    assert "مبالغ مستحقة على العملاء" in catalog
    assert "showOutstandingBalances" in catalog


def test_reports_surface_uses_linka_interaction_tokens_not_legacy_teal() -> None:
    for relative in (
        "frontend/src/app/(dashboard)/analytics/page.tsx",
        "frontend/src/app/(dashboard)/analytics/overview.tsx",
        "frontend/src/app/(dashboard)/analytics/catalog.tsx",
        "frontend/src/app/(dashboard)/analytics/dashboard-charts.tsx",
        "frontend/src/app/(dashboard)/analytics/outstanding-balances/page.tsx",
    ):
        assert "teal-" not in _read(relative)


def test_reports_copy_is_product_facing_not_internal_implementation_copy() -> None:
    catalog = _read("frontend/src/app/(dashboard)/analytics/catalog.tsx")

    assert "التقارير هنا جرافات وبطاقات فقط" not in catalog
    assert "مفيش بيانات مطابقة" not in catalog
    assert "ماذا يوضح؟" in catalog
    assert "طريقة الحساب" in catalog
