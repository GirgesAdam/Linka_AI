from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas.analytics_bi import AnalyticsBIMetricRead
from app.schemas.analytics_business import AnalyticsBusinessPlan
from app.schemas.analytics_catalog import (
    AnalyticsCatalogChartDataRead,
    AnalyticsCatalogChartSeriesRead,
    AnalyticsCatalogDefinitionRead,
    AnalyticsCatalogRead,
    AnalyticsCatalogRunRead,
    AnalyticsCatalogRunRequest,
)
from app.schemas.analytics_composable import AnalyticsAudiencePlan
from app.services.analytics_audience import execute_audience_plan, validate_audience_plan_entities
from app.services.analytics_bi import AnalyticsBIError, analytics_entity_catalog
from app.services.analytics_business import execute_business_plan, validate_business_plan_entities
from app.services.analytics_catalog_special import execute_special_catalog_analysis
from app.services.analytics_runtime import (
    get_cached_aggregate,
    log_catalog_execution,
    put_cached_aggregate,
)

Mode = Literal["business", "audience", "custom"]


@dataclass(frozen=True)
class _Definition:
    key: str
    category: str
    title: str
    description: str
    result_kind: str
    default_chart: str
    supported_charts: tuple[str, ...]
    filters: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    mode: Mode
    metrics: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    default_lookback_days: int | None = 30
    default_granularity: str | None = None
    default_inactivity_days: int | None = None
    default_limit: int = 10
    default_min_visits: int | None = None
    default_max_visits: int | None = None
    chart_metric_keys: tuple[str, ...] = ()
    audience_defaults: tuple[tuple[str, object], ...] = ()
    highlight_metrics: tuple[str, ...] = ()

    def read(self) -> AnalyticsCatalogDefinitionRead:
        return AnalyticsCatalogDefinitionRead(
            key=self.key,
            category=self.category,
            title=self.title,
            description=self.description,
            result_kind=self.result_kind,
            default_chart=self.default_chart,
            supported_charts=list(self.supported_charts),
            filters=list(self.filters),
            allowed_actions=list(self.allowed_actions),
            default_lookback_days=self.default_lookback_days,
            default_granularity=self.default_granularity,
            default_inactivity_days=self.default_inactivity_days,
            default_limit=self.default_limit,
            default_min_visits=self.default_min_visits,
            default_max_visits=self.default_max_visits,
            chart_metric_keys=list(self.chart_metric_keys),
        )


_PERIOD = ("period",)
_ENTITY = ("service", "branch", "doctor")
_LIMIT = ("limit",)
# Linka currently operates in EGP only. Money remains explicit in the domain layer,
# but currency is not a user-facing analytics filter.
_ANALYTICS_CURRENCY = "EGP"
_MONEY: tuple[str, ...] = ()
_COMPARISON = ("comparison",)
_GRANULARITY = ("granularity",)


_DEFINITIONS: tuple[_Definition, ...] = (
    # Revenue
    _Definition(
        "revenue_overview", "revenue", "ملخص الإيرادات",
        "إجمالي المقبوضات والمرتجعات وصافي المدفوعات في الفترة المختارة.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _MONEY + _COMPARISON,
        ("export",), "business",
        metrics=("gross_paid_minor", "refunded_minor", "net_paid_minor"),
        default_lookback_days=30,
        chart_metric_keys=("gross_paid_minor", "refunded_minor", "net_paid_minor"),
    ),
    _Definition(
        "revenue_trend", "revenue", "تطور الإيرادات",
        "تغير صافي المدفوعات عبر الوقت، مع إمكانية تضييق النتائج حسب الخدمة أو الفرع أو الدكتور.",
        "trend", "line", ("line", "bar", "table"), _PERIOD + _ENTITY + _MONEY + _GRANULARITY,
        ("export",), "business",
        metrics=("net_paid_minor",), default_lookback_days=180, default_granularity="month",
        chart_metric_keys=("net_paid_minor",), highlight_metrics=("net_paid_minor",),
    ),
    _Definition(
        "revenue_by_service", "revenue", "الإيراد حسب الخدمة",
        "صافي المدفوع المنسوب صراحةً لكل خدمة من المواعيد المدفوعة ومبيعات الباقات المرتبطة بالخدمة.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("branch", "doctor") + _MONEY + _COMPARISON + _LIMIT,
        ("export",), "business",
        metrics=("net_paid_minor", "appointments", "completed_appointments"), group_by=("service",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("net_paid_minor",),
    ),
    _Definition(
        "revenue_by_doctor", "revenue", "الإيراد حسب الدكتور",
        "صافي المدفوع المرتبط فعليًا بمواعيد كل دكتور.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("service", "branch") + _MONEY + _COMPARISON + _LIMIT,
        ("export",), "business",
        metrics=("net_paid_minor", "appointments", "completed_appointments"), group_by=("doctor",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("net_paid_minor",),
    ),
    _Definition(
        "revenue_by_branch", "revenue", "الإيراد حسب الفرع",
        "صافي المدفوع المرتبط فعليًا بمواعيد كل فرع.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("service", "doctor") + _MONEY + _COMPARISON + _LIMIT,
        ("export",), "business",
        metrics=("net_paid_minor", "appointments", "completed_appointments"), group_by=("branch",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("net_paid_minor",),
    ),
    _Definition(
        "average_patient_value", "revenue", "متوسط ما دفعه العميل",
        "متوسط صافي المدفوعات لكل عميل دفع خلال الفترة المختارة.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _MONEY + _COMPARISON,
        ("export",), "business",
        metrics=("avg_net_paid_per_paying_patient_minor", "paying_patients", "net_paid_minor"),
        default_lookback_days=90, chart_metric_keys=("avg_net_paid_per_paying_patient_minor",),
    ),

    # Patients
    _Definition(
        "new_patients_trend", "patients", "العملاء الجدد",
        "عدد العملاء الجدد حسب تاريخهم الأصلي عند توفره، وإلا تاريخ إنشائهم في Linka.",
        "trend", "line", ("line", "bar", "table"), _PERIOD + _GRANULARITY,
        ("export",), "business",
        metrics=("new_patients",), default_lookback_days=180, default_granularity="month",
        chart_metric_keys=("new_patients",), highlight_metrics=("new_patients",),
    ),
    _Definition(
        "repeat_patient_rate", "patients", "نسبة العملاء العائدين",
        "العملاء الذين لديهم زيارتان مكتملتان أو أكثر داخل الفترة المختارة.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _ENTITY + _COMPARISON,
        ("export",), "business",
        metrics=("unique_patients", "repeat_patients", "repeat_rate"), default_lookback_days=365,
        chart_metric_keys=("repeat_rate",),
    ),
    _Definition(
        "lapsed_patients", "patients", "العملاء المنقطعون",
        "عملاء آخر جلسة مكتملة لهم أقدم من المدة المحددة ولا يوجد لهم حجز نشط قادم.",
        "patient_list", "table", ("table",), ("service", "branch", "doctor", "inactivity_days", "limit"),
        ("export", "save_patient_group", "follow_up_tasks", "whatsapp_campaign"), "audience",
        default_lookback_days=None, default_inactivity_days=180, default_limit=25,
        audience_defaults=(("has_future_appointment", False), ("sort_by", "last_activity_asc")),
    ),
    _Definition(
        "high_value_patients", "patients", "أعلى العملاء من حيث المدفوعات",
        "ترتيب العملاء حسب صافي المدفوعات الفعلية خلال الفترة المختارة.",
        "patient_list", "table", ("table",), _PERIOD + _ENTITY + _MONEY + _LIMIT,
        ("export", "save_patient_group", "follow_up_tasks", "whatsapp_campaign"), "audience",
        default_lookback_days=365, default_limit=25,
        audience_defaults=(("sort_by", "net_paid_desc"), ("min_net_paid_minor", 0)),
    ),
    _Definition(
        "most_frequent_patients", "patients", "العملاء الأكثر زيارة",
        "أعلى العملاء حسب عدد الجلسات المكتملة المطابقة خلال الفترة.",
        "patient_list", "table", ("table",), _PERIOD + _ENTITY + _LIMIT,
        ("export", "save_patient_group", "follow_up_tasks", "whatsapp_campaign"), "audience",
        default_lookback_days=365, default_limit=25,
        audience_defaults=(("sort_by", "matching_visits_desc"),),
    ),
    _Definition(
        "one_visit_patients", "patients", "عملاء زاروا مرة واحدة",
        "العملاء الذين لديهم جلسة مكتملة واحدة فقط مطابقة داخل الفترة المختارة.",
        "patient_list", "table", ("table",), _PERIOD + _ENTITY + _LIMIT,
        ("export", "save_patient_group", "follow_up_tasks", "whatsapp_campaign"), "audience",
        default_lookback_days=365, default_limit=25,
        audience_defaults=(("max_matching_visits", 1), ("sort_by", "last_activity_asc")),
    ),
    _Definition(
        "patients_by_visit_count", "patients", "فلترة العملاء بعدد الزيارات",
        "حدد أقل وأقصى عدد جلسات مكتملة مطابقة داخل الفترة لإخراج قائمة العملاء.",
        "patient_list", "table", ("table",), _PERIOD + _ENTITY + ("min_visits", "max_visits") + _LIMIT,
        ("export", "save_patient_group", "follow_up_tasks", "whatsapp_campaign"), "audience",
        default_lookback_days=365, default_limit=25, default_min_visits=2,
        audience_defaults=(("min_matching_visits", 2), ("sort_by", "matching_visits_desc")),
    ),
    _Definition(
        "patients_without_future_booking", "patients", "عملاء بدون حجز قادم",
        "عملاء لديهم جلسة مكتملة في الفترة ولا يوجد لهم موعد نشط قادم.",
        "patient_list", "table", ("table",), _PERIOD + _ENTITY + ("marketing_consent",) + _LIMIT,
        ("export", "save_patient_group", "follow_up_tasks", "whatsapp_campaign"), "audience",
        default_lookback_days=180, default_limit=25,
        audience_defaults=(("has_future_appointment", False), ("sort_by", "last_activity_desc")),
    ),

    # Appointments
    _Definition(
        "appointment_overview", "appointments", "ملخص المواعيد",
        "إجمالي المواعيد، والجلسات المكتملة، وعدم الحضور، والإلغاءات ونسبها خلال الفترة.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _ENTITY + _COMPARISON,
        ("export",), "business",
        metrics=("appointments", "completed_appointments", "no_show_appointments", "cancelled_appointments", "attendance_rate", "cancellation_rate"),
        default_lookback_days=30,
        chart_metric_keys=("appointments", "completed_appointments", "no_show_appointments", "cancelled_appointments"),
    ),
    _Definition(
        "appointment_trend", "appointments", "تطور المواعيد",
        "تغير المواعيد والجلسات المكتملة وعدم الحضور والإلغاءات عبر الوقت.",
        "trend", "line", ("line", "bar", "table"), _PERIOD + _ENTITY + _GRANULARITY,
        ("export",), "business",
        metrics=("appointments", "completed_appointments", "no_show_appointments", "cancelled_appointments"),
        default_lookback_days=90, default_granularity="week", chart_metric_keys=("appointments", "completed_appointments"), highlight_metrics=("appointments", "completed_appointments", "no_show_appointments"),
    ),
    _Definition(
        "doctor_appointment_performance", "doctors", "أداء الدكاترة في المواعيد",
        "حجم المواعيد ونسب إتمام الجلسات وعدم الحضور والإلغاء لكل دكتور.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("service", "branch") + _LIMIT,
        ("export",), "business",
        metrics=("appointments", "completion_rate", "no_show_rate", "cancellation_rate"), group_by=("doctor",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("appointments", "completion_rate"), highlight_metrics=("appointments", "completion_rate", "no_show_rate"),
    ),
    _Definition(
        "branch_appointment_performance", "branches", "أداء الفروع في المواعيد",
        "حجم المواعيد ونسب إتمام الجلسات وعدم الحضور والإلغاء لكل فرع.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("service", "doctor") + _LIMIT,
        ("export",), "business",
        metrics=("appointments", "completion_rate", "no_show_rate", "cancellation_rate"), group_by=("branch",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("appointments", "completion_rate"), highlight_metrics=("appointments", "completion_rate", "no_show_rate"),
    ),
    _Definition(
        "appointment_source_performance", "appointments", "الأداء حسب مصدر الحجز",
        "مقارنة مصادر الحجز مثل WhatsApp والهاتف والويب حسب الحجم ونسب إتمام الجلسات وعدم الحضور.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + _ENTITY + _LIMIT,
        ("export",), "business",
        metrics=("appointments", "completion_rate", "no_show_rate", "cancellation_rate"), group_by=("source",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("appointments", "completion_rate"), highlight_metrics=("appointments", "completion_rate", "no_show_rate"),
    ),

    _Definition(
        "appointment_peak_weekdays", "appointments", "أكثر أيام الأسبوع ازدحامًا",
        "رتّب أيام الأسبوع حسب حجم المواعيد لمعرفة الأيام التي تحتاج سعة أكبر.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + _ENTITY,
        ("export",), "custom", default_lookback_days=90, chart_metric_keys=("appointments",),
    ),
    _Definition(
        "appointment_peak_hours", "appointments", "خريطة أوقات الذروة",
        "اعرف ساعات الحجز الأكثر ضغطًا في كل يوم من أيام الأسبوع.",
        "breakdown", "heatmap", ("heatmap", "table"), _PERIOD + _ENTITY,
        ("export",), "custom", default_lookback_days=90, chart_metric_keys=("appointments",),
    ),
    _Definition(
        "no_show_peak_times", "appointments", "أوقات عدم الحضور الأعلى",
        "خريطة توضح الأيام والساعات التي ترتفع فيها نسبة عدم حضور العميل.",
        "breakdown", "heatmap", ("heatmap", "table"), _PERIOD + _ENTITY,
        ("export",), "custom", default_lookback_days=180, chart_metric_keys=("no_show_rate",),
    ),
    _Definition(
        "cancellation_peak_times", "appointments", "أوقات الإلغاء الأعلى",
        "خريطة توضح الأيام والساعات التي ترتفع فيها نسبة إلغاء المواعيد.",
        "breakdown", "heatmap", ("heatmap", "table"), _PERIOD + _ENTITY,
        ("export",), "custom", default_lookback_days=180, chart_metric_keys=("cancellation_rate",),
    ),

    # Services / retention
    _Definition(
        "service_popularity", "services", "الخدمات الأكثر حجزًا",
        "ترتيب الخدمات حسب عدد المواعيد والعملاء الفريدين والجلسات المكتملة.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("branch", "doctor") + _LIMIT,
        ("export",), "business",
        metrics=("appointments", "unique_patients", "completed_appointments"), group_by=("service",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("appointments",), highlight_metrics=("appointments", "unique_patients", "completed_appointments"),
    ),
    _Definition(
        "service_retention", "retention", "عودة العملاء لنفس الخدمة",
        "نسبة عملاء كل خدمة الذين عادوا لجلسة مكتملة ثانية لنفس الخدمة داخل الفترة.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("branch", "doctor") + _LIMIT,
        ("export",), "business",
        metrics=("same_service_repeat_rate", "unique_patients", "repeat_patients"), group_by=("service",),
        default_lookback_days=365, default_limit=10, chart_metric_keys=("same_service_repeat_rate",),
    ),
    _Definition(
        "service_completion", "services", "نسبة إتمام الجلسات حسب الخدمة",
        "مقارنة نسبة إتمام الجلسات وعدم الحضور لكل خدمة.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("branch", "doctor") + _LIMIT,
        ("export",), "business",
        metrics=("completion_rate", "appointments", "no_show_rate"), group_by=("service",),
        default_lookback_days=90, default_limit=10, chart_metric_keys=("completion_rate",), highlight_metrics=("appointments", "completion_rate", "no_show_rate"),
    ),
    _Definition(
        "doctor_retention", "retention", "عودة العملاء لنفس الدكتور",
        "نسبة عملاء كل دكتور الذين عادوا لزيارة مكتملة ثانية مع نفس الدكتور داخل الفترة.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("service", "branch") + _LIMIT,
        ("export",), "business",
        metrics=("repeat_rate", "unique_patients", "repeat_patients"), group_by=("doctor",),
        default_lookback_days=365, default_limit=10, chart_metric_keys=("repeat_rate",),
    ),

    _Definition(
        "branch_retention", "retention", "عودة العملاء لنفس الفرع",
        "نسبة عملاء كل فرع الذين عادوا لزيارة مكتملة ثانية داخل نفس الفرع.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("service", "doctor") + _LIMIT,
        ("export",), "business",
        metrics=("repeat_rate", "unique_patients", "repeat_patients"), group_by=("branch",),
        default_lookback_days=365, default_limit=10, chart_metric_keys=("repeat_rate",),
    ),
    _Definition(
        "second_visit_conversion", "retention", "التحويل للزيارة الثانية",
        "من العملاء الذين أكملوا زيارة، اعرف كام واحد وصل لزيارة مكتملة ثانية داخل الفترة نفسها.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _ENTITY,
        ("export",), "custom", default_lookback_days=365,
    ),
    _Definition(
        "third_visit_conversion", "retention", "التحويل للزيارة الثالثة",
        "من العملاء الذين أكملوا زيارة، اعرف كام واحد وصل لثلاث زيارات مكتملة داخل الفترة نفسها.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _ENTITY,
        ("export",), "custom", default_lookback_days=365,
    ),
    _Definition(
        "time_to_return", "retention", "الوقت المعتاد لعودة العميل",
        "المدة بين أول وثاني زيارة مكتملة، مع عرض الوسيط والمتوسط حتى تكون الصورة أوضح.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _ENTITY,
        ("export",), "custom", default_lookback_days=730,
    ),
    _Definition(
        "time_to_return_by_service", "retention", "وقت العودة حسب الخدمة",
        "قارن الوقت المعتاد الذي يحتاجه العميل للعودة لنفس الخدمة مرة ثانية.",
        "breakdown", "bar", ("bar", "table"), _PERIOD + ("branch", "doctor") + _LIMIT,
        ("export",), "custom", default_lookback_days=730, default_limit=10,
        chart_metric_keys=("median_days_to_second_visit", "avg_days_to_second_visit"),
    ),
    _Definition(
        "lapsed_rate", "retention", "نسبة العملاء المنقطعين",
        "نسبة العملاء الذين مر على آخر زيارة مكتملة لهم المدة المحددة ولا يوجد لهم حجز نشط قادم.",
        "summary", "kpi", ("kpi", "table"), _PERIOD + _ENTITY + ("inactivity_days",),
        ("export",), "custom", default_lookback_days=None, default_inactivity_days=180,
    ),

    # Funnels
    _Definition(
        "booking_completion_funnel", "funnels", "الحجز → إتمام الجلسة",
        "يوضح كام حجز وصل فعليًا إلى جلسة مكتملة.",
        "funnel", "funnel", ("funnel", "table"), _PERIOD + _ENTITY,
        ("export",), "business",
        metrics=("appointments", "completed_appointments", "completion_rate"), default_lookback_days=90,
        chart_metric_keys=("appointments", "completed_appointments"),
    ),
    _Definition(
        "booking_paid_funnel", "funnels", "الحجز → إتمام الجلسة → الدفع",
        "يوضح كام حجز وصل لجلسة مكتملة، ثم كام جلسة مكتملة لها دفعة مسجلة فعليًا.",
        "funnel", "funnel", ("funnel", "table"), _PERIOD + _ENTITY,
        ("export",), "business",
        metrics=("appointments", "completed_appointments", "paid_completed_appointments", "completion_rate", "paid_completion_rate", "booking_to_paid_rate"),
        default_lookback_days=90,
        chart_metric_keys=("appointments", "completed_appointments", "paid_completed_appointments"),
    ),
)

_BY_KEY = {definition.key: definition for definition in _DEFINITIONS}


def analytics_catalog(db: Session, *, workspace_id: UUID) -> AnalyticsCatalogRead:
    entities = analytics_entity_catalog(db, workspace_id=workspace_id)
    return AnalyticsCatalogRead(
        analyses=[definition.read() for definition in _DEFINITIONS],
        services=entities.get("services", []),
        branches=entities.get("branches", []),
        doctors=entities.get("doctors", []),
    )


def _period_values(definition: _Definition, request: AnalyticsCatalogRunRequest) -> tuple[int | None, object | None, object | None]:
    if "period" not in definition.filters:
        if request.lookback_days is not None or request.all_history or request.start_date is not None or request.end_date is not None:
            raise AnalyticsBIError(f"{definition.key} does not accept a period filter.")
        return definition.default_lookback_days, None, None
    if request.all_history:
        return None, None, None
    if request.start_date is not None or request.end_date is not None:
        if definition.mode == "audience":
            raise AnalyticsBIError("Custom start/end dates are not supported for patient-list analyses yet.")
        return None, request.start_date, request.end_date
    return request.lookback_days if request.lookback_days is not None else definition.default_lookback_days, None, None


def _reject_unsupported(definition: _Definition, request: AnalyticsCatalogRunRequest) -> None:
    supported = set(definition.filters)
    checks = {
        "service": bool(request.service_ids),
        "branch": bool(request.branch_ids),
        "doctor": bool(request.doctor_ids),
        "comparison": request.comparison,
        "granularity": request.granularity is not None,
        "limit": request.limit is not None,
        "inactivity_days": request.inactivity_days is not None,
        "min_visits": request.min_visits is not None,
        "max_visits": request.max_visits is not None,
        "future_booking": request.has_future_appointment is not None,
        "marketing_consent": request.marketing_consent is not None,
    }
    for key, supplied in checks.items():
        if supplied and key not in supported:
            raise AnalyticsBIError(f"{definition.key} does not accept the {key} filter.")




def validate_catalog_request(
    db: Session,
    *,
    workspace_id: UUID,
    request: AnalyticsCatalogRunRequest,
    catalog: dict[str, list[dict[str, str]]] | None = None,
) -> _Definition:
    """Validate a catalog request without executing the analysis.

    Saved views use this to persist only requests that the current deterministic
    registry accepts. Canonical entity ids are checked in the current workspace.
    """
    definition = _BY_KEY.get(request.analysis_key)
    if definition is None:
        raise AnalyticsBIError("Unknown analytics catalog analysis key.")
    _reject_unsupported(definition, request)
    _period_values(definition, request)
    if catalog is None:
        catalog = analytics_entity_catalog(db, workspace_id=workspace_id)
    _validate_request_entities(request, catalog=catalog)
    return definition


def materialize_catalog_request(
    definition: _Definition,
    request: AnalyticsCatalogRunRequest,
) -> AnalyticsCatalogRunRequest:
    """Return the effective request with registry defaults made explicit.

    A saved view should keep the period/limit/granularity that the admin actually
    ran, rather than silently inheriting different defaults after a future release.
    """
    updates: dict[str, object] = {}
    supported = set(definition.filters)
    if "period" in supported and not request.all_history and request.lookback_days is None and request.start_date is None:
        if definition.default_lookback_days is None:
            updates["all_history"] = True
        else:
            updates["lookback_days"] = definition.default_lookback_days
    if "granularity" in supported and request.granularity is None and definition.default_granularity is not None:
        updates["granularity"] = definition.default_granularity
    if "limit" in supported and request.limit is None:
        updates["limit"] = definition.default_limit
    if "inactivity_days" in supported and request.inactivity_days is None and definition.default_inactivity_days is not None:
        updates["inactivity_days"] = definition.default_inactivity_days
    if "min_visits" in supported and request.min_visits is None and definition.default_min_visits is not None:
        updates["min_visits"] = definition.default_min_visits
    if "max_visits" in supported and request.max_visits is None and definition.default_max_visits is not None:
        updates["max_visits"] = definition.default_max_visits
    return request.model_copy(update=updates) if updates else request


def _business_plan(definition: _Definition, request: AnalyticsCatalogRunRequest) -> AnalyticsBusinessPlan:
    lookback_days, start_date, end_date = _period_values(definition, request)
    group_by = list(definition.group_by)
    if definition.default_granularity is not None:
        granularity = request.granularity or definition.default_granularity
        group_by = [granularity if value in {"day", "week", "month"} else value for value in group_by]
        if not group_by:
            group_by = [granularity]
    currency = _ANALYTICS_CURRENCY if any(metric.endswith("_minor") for metric in definition.metrics) else None
    return AnalyticsBusinessPlan(
        kind="business_analytics",
        metrics=list(definition.metrics),
        group_by=group_by,
        lookback_days=lookback_days,
        start_date=start_date,
        end_date=end_date,
        comparison="previous_period" if request.comparison else "none",
        service_ids=request.service_ids,
        branch_ids=request.branch_ids,
        doctor_ids=request.doctor_ids,
        currency=currency,
        limit=request.limit or definition.default_limit,
        sort_metric=definition.metrics[0],
        sort_direction="desc",
        reason=f"Admin selected catalog analysis: {definition.key}",
    )


def _audience_plan(definition: _Definition, request: AnalyticsCatalogRunRequest) -> AnalyticsAudiencePlan:
    lookback_days, _, _ = _period_values(definition, request)
    defaults = dict(definition.audience_defaults)
    default_min_visits = int(defaults.get("min_matching_visits", 1))
    min_visits = request.min_visits or default_min_visits
    max_visits = request.max_visits
    if "max_matching_visits" in defaults and request.max_visits is None:
        max_visits = int(defaults["max_matching_visits"])
    has_future = request.has_future_appointment
    if has_future is None and "has_future_appointment" in defaults:
        has_future = bool(defaults["has_future_appointment"])
    min_net = defaults.get("min_net_paid_minor")
    currency = _ANALYTICS_CURRENCY if defaults.get("sort_by") == "net_paid_desc" else None
    return AnalyticsAudiencePlan(
        kind="patient_audience",
        lookback_days=lookback_days,
        inactivity_days=(
            request.inactivity_days
            if request.inactivity_days is not None
            else definition.default_inactivity_days
        ),
        limit=request.limit or definition.default_limit,
        service_ids=request.service_ids,
        branch_ids=request.branch_ids,
        doctor_ids=request.doctor_ids,
        appointment_statuses=["completed"],
        min_matching_visits=min_visits,
        max_matching_visits=max_visits,
        has_future_appointment=has_future,
        marketing_consent=request.marketing_consent,
        patient_statuses=["active", "inactive"],
        min_net_paid_minor=int(min_net) if min_net is not None else None,
        max_net_paid_minor=None,
        currency=currency,
        sort_by=str(defaults.get("sort_by", "last_activity_desc")),
        reason=f"Admin selected catalog analysis: {definition.key}",
    )


def _catalog_definitions(definitions: list[str], *, mode: Mode) -> list[str]:
    cleaned: list[str] = []
    for text in definitions:
        if "الـAI" in text or "AI" in text:
            continue
        cleaned.append(
            text.replace("الخطة", "الفلاتر المختارة")
            .replace("في العملة المحددة", "بالجنيه المصري")
        )
    prefix = (
        "الأرقام محسوبة من البيانات المسجلة في Linka وبنفس التعريف كل مرة."
        if mode != "audience"
        else "قائمة العملاء محسوبة من نفس الشروط الظاهرة، وتُعاد مراجعتها عند تنفيذ أي إجراء عليها."
    )
    return [prefix, *cleaned]


def _series_format(*, key: str, currency: str | None) -> str:
    if currency:
        return "money"
    if "rate" in key or "percent" in key or "change_percent" in key:
        return "percent"
    return "number"


def _chart_data(
    rows: list,
    *,
    result_kind: str,
    metric_keys: tuple[str, ...],
    title: str,
) -> AnalyticsCatalogChartDataRead:
    if not rows or not metric_keys:
        return AnalyticsCatalogChartDataRead(labels=[], series=[])

    if result_kind == "funnel":
        first = rows[0]
        metrics = [next((metric for metric in first.metrics if metric.key == key), None) for key in metric_keys]
        present = [metric for metric in metrics if metric is not None and isinstance(metric.value, (int, float))]
        return AnalyticsCatalogChartDataRead(
            labels=[metric.label for metric in present],
            series=[
                AnalyticsCatalogChartSeriesRead(
                    key="funnel",
                    label=title,
                    format="number",
                    currency=None,
                    values=[metric.value for metric in present],
                )
            ] if present else [],
        )

    series: list[AnalyticsCatalogChartSeriesRead] = []
    for key in metric_keys:
        sample = next(
            (metric for row in rows for metric in row.metrics if metric.key == key),
            None,
        )
        if sample is None:
            continue
        values: list[int | float | None] = []
        has_numeric = False
        for row in rows:
            metric = next((item for item in row.metrics if item.key == key), None)
            if metric is not None and isinstance(metric.value, (int, float)):
                values.append(metric.value)
                has_numeric = True
            else:
                values.append(None)
        if has_numeric:
            series.append(
                AnalyticsCatalogChartSeriesRead(
                    key=key,
                    label=sample.label,
                    format=_series_format(key=key, currency=sample.currency),
                    currency=sample.currency,
                    values=values,
                )
            )
    return AnalyticsCatalogChartDataRead(labels=[row.label for row in rows], series=series)


def _business_highlights(
    db: Session,
    *,
    workspace_id: UUID,
    definition: _Definition,
    plan: AnalyticsBusinessPlan,
    now: datetime | None,
) -> list[AnalyticsBIMetricRead]:
    """Return aggregate headline metrics for grouped/trend analyses.

    The highlight query deliberately reuses the same validated filters and
    metric definitions, but removes the grouping dimension. This avoids
    unsafe frontend summation of rates or unique-patient counts.
    """
    if not definition.highlight_metrics:
        return []
    payload = plan.model_dump(mode="python")
    payload.update(
        metrics=list(definition.highlight_metrics),
        group_by=[],
        limit=1,
        sort_metric=definition.highlight_metrics[0],
    )
    highlight_plan = AnalyticsBusinessPlan.model_validate(payload)
    highlight_result = execute_business_plan(
        db,
        workspace_id=workspace_id,
        question=f"{definition.title} · summary",
        plan=highlight_plan,
        model=None,
        now=now,
    )
    return list(highlight_result.rows[0].metrics) if highlight_result.rows else []


def _validate_request_entities(
    request: AnalyticsCatalogRunRequest, *, catalog: dict[str, list[dict[str, str]]]
) -> None:
    for field_name, collection_name in (
        ("service_ids", "services"),
        ("branch_ids", "branches"),
        ("doctor_ids", "doctors"),
    ):
        allowed = {str(item.get("id")) for item in catalog.get(collection_name, [])}
        invalid = [value for value in getattr(request, field_name) if value not in allowed]
        if invalid:
            raise AnalyticsBIError(
                f"Analytics request referenced an unknown canonical {collection_name[:-1]} id."
            )


def run_catalog_analysis(
    db: Session,
    *,
    workspace_id: UUID,
    request: AnalyticsCatalogRunRequest,
    now: datetime | None = None,
    use_cache: bool = True,
) -> AnalyticsCatalogRunRead:
    started_at = perf_counter()
    catalog = analytics_entity_catalog(db, workspace_id=workspace_id)
    definition = validate_catalog_request(
        db,
        workspace_id=workspace_id,
        request=request,
        catalog=catalog,
    )
    request = materialize_catalog_request(definition, request)

    # Only aggregate results are cacheable. Patient lists contain PII and are
    # intentionally re-executed every time because they can drive CRM actions.
    if use_cache and definition.result_kind != "patient_list":
        cached = get_cached_aggregate(workspace_id=workspace_id, request=request, as_of=now)
        if cached is not None:
            log_catalog_execution(
                workspace_id=workspace_id,
                analysis_key=definition.key,
                started_at=started_at,
                cache_hit=True,
                rows=len(cached.rows),
            )
            return cached

    if definition.mode == "business":
        plan = validate_business_plan_entities(_business_plan(definition, request), catalog=catalog)
        result = execute_business_plan(
            db,
            workspace_id=workspace_id,
            question=definition.title,
            plan=plan,
            model=None,
            now=now,
        )
        rows = list(result.rows)
        if definition.result_kind == "trend":
            rows.sort(key=lambda row: row.label)
        response = AnalyticsCatalogRunRead(
            request=request,
            analysis_key=definition.key,
            title=definition.title,
            category=definition.category,
            result_kind=definition.result_kind,
            chart=definition.default_chart,
            supported_charts=list(definition.supported_charts),
            chart_metric_keys=list(definition.chart_metric_keys),
            chart_data=_chart_data(rows, result_kind=definition.result_kind, metric_keys=definition.chart_metric_keys, title=definition.title),
            highlights=_business_highlights(
                db, workspace_id=workspace_id, definition=definition, plan=plan, now=now
            ),
            allowed_actions=list(definition.allowed_actions),
            period_label=result.period_label,
            answer=f"{definition.title}: {len(rows)} نتيجة.",
            definitions=_catalog_definitions(result.definitions, mode="business"),
            rows=rows,
            business_plan=plan,
            audience_plan=None,
        )
    elif definition.mode == "custom":
        _validate_request_entities(request, catalog=catalog)
        result = execute_special_catalog_analysis(
            db,
            workspace_id=workspace_id,
            analysis_key=definition.key,
            request=request,
            default_lookback_days=definition.default_lookback_days,
            now=now,
        )
        response = AnalyticsCatalogRunRead(
            request=request,
            analysis_key=definition.key,
            title=definition.title,
            category=definition.category,
            result_kind=definition.result_kind,
            chart=definition.default_chart,
            supported_charts=list(definition.supported_charts),
            chart_metric_keys=list(definition.chart_metric_keys),
            chart_data=result.chart_data,
            highlights=result.highlights,
            allowed_actions=list(definition.allowed_actions),
            period_label=result.period_label,
            answer=result.answer,
            definitions=_catalog_definitions(result.definitions, mode="custom"),
            rows=result.rows,
            business_plan=None,
            audience_plan=None,
        )
    else:
        plan = validate_audience_plan_entities(_audience_plan(definition, request), catalog=catalog)
        result = execute_audience_plan(db, workspace_id=workspace_id, plan=plan, now=now)
        response = AnalyticsCatalogRunRead(
            request=request,
            analysis_key=definition.key,
            title=definition.title,
            category=definition.category,
            result_kind=definition.result_kind,
            chart=definition.default_chart,
            supported_charts=list(definition.supported_charts),
            chart_metric_keys=list(definition.chart_metric_keys),
            chart_data=_chart_data(list(result.rows), result_kind=definition.result_kind, metric_keys=definition.chart_metric_keys, title=definition.title),
            highlights=[],
            allowed_actions=list(definition.allowed_actions),
            period_label=result.period_label,
            answer=result.answer,
            definitions=_catalog_definitions(result.definitions, mode="audience"),
            rows=result.rows,
            business_plan=None,
            audience_plan=plan,
        )

    if use_cache and definition.result_kind != "patient_list":
        put_cached_aggregate(workspace_id=workspace_id, request=request, result=response, as_of=now)
    log_catalog_execution(
        workspace_id=workspace_id,
        analysis_key=definition.key,
        started_at=started_at,
        cache_hit=False,
        rows=len(response.rows),
    )
    return response
