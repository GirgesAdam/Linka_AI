from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, extract, func, select
from sqlalchemy.orm import Session

from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.service import Service
from app.models.staff import Staff
from app.schemas.analytics_bi import AnalyticsBIMetricRead, AnalyticsBIResultRow
from app.schemas.analytics_catalog import (
    AnalyticsCatalogChartDataRead,
    AnalyticsCatalogChartSeriesRead,
    AnalyticsCatalogRunRequest,
)
from app.services.analytics_bi import AnalyticsBIError


@dataclass(frozen=True)
class CatalogSpecialResult:
    period_label: str
    answer: str
    definitions: list[str]
    rows: list[AnalyticsBIResultRow]
    chart_data: AnalyticsCatalogChartDataRead
    highlights: list[AnalyticsBIMetricRead]


@dataclass(frozen=True)
class _Period:
    start: datetime | None
    end: datetime
    label: str


_WEEKDAYS: tuple[tuple[int, str], ...] = (
    (0, "الأحد"),
    (1, "الاثنين"),
    (2, "الثلاثاء"),
    (3, "الأربعاء"),
    (4, "الخميس"),
    (5, "الجمعة"),
    (6, "السبت"),
)
_WEEKDAY_LABELS = dict(_WEEKDAYS)


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current


def _period(
    request: AnalyticsCatalogRunRequest,
    *,
    default_lookback_days: int | None,
    now: datetime | None,
) -> _Period:
    current = _now(now)
    tz = current.tzinfo or UTC
    if request.all_history:
        return _Period(start=None, end=current, label="كل التاريخ المتاح")
    if request.start_date is not None and request.end_date is not None:
        start = datetime.combine(request.start_date, time.min, tzinfo=tz)
        end = datetime.combine(request.end_date + timedelta(days=1), time.min, tzinfo=tz)
        return _Period(start=start, end=end, label=f"من {request.start_date.isoformat()} إلى {request.end_date.isoformat()}")
    days = request.lookback_days if request.lookback_days is not None else default_lookback_days
    if days is None:
        return _Period(start=None, end=current, label="كل التاريخ المتاح")
    return _Period(start=current - timedelta(days=days), end=current, label=f"آخر {days} يوم")


def _as_uuids(values: list[str]) -> list[UUID]:
    result: list[UUID] = []
    for value in values:
        try:
            result.append(UUID(value))
        except ValueError as exc:
            raise AnalyticsBIError("Analytics request contains an invalid canonical UUID.") from exc
    return result


def _appointment_filters(
    *, workspace_id: UUID, request: AnalyticsCatalogRunRequest, period: _Period, completed_only: bool = False
) -> list[Any]:
    clauses: list[Any] = [Appointment.workspace_id == workspace_id]
    if completed_only:
        clauses.append(Appointment.status == "completed")
    else:
        clauses.append(Appointment.status != "rescheduled")
    if period.start is not None:
        clauses.append(Appointment.start_at >= period.start)
    clauses.append(Appointment.start_at < period.end)
    if request.service_ids:
        clauses.append(Appointment.service_id.in_(_as_uuids(request.service_ids)))
    if request.branch_ids:
        clauses.append(Appointment.branch_id.in_(_as_uuids(request.branch_ids)))
    if request.doctor_ids:
        clauses.append(Appointment.doctor_id.in_(_as_uuids(request.doctor_ids)))
    return clauses


def _metric(key: str, label: str, value: int | float) -> AnalyticsBIMetricRead:
    return AnalyticsBIMetricRead(key=key, label=label, value=value)


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 1)


def _extract_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(float(value))


def _peak_weekdays(
    db: Session, *, workspace_id: UUID, request: AnalyticsCatalogRunRequest, period: _Period
) -> CatalogSpecialResult:
    dow = extract("dow", Appointment.start_at).label("weekday")
    stmt = (
        select(
            dow,
            func.count(Appointment.id).label("appointments"),
            func.sum(case((Appointment.status == "completed", 1), else_=0)).label("completed"),
        )
        .where(*_appointment_filters(workspace_id=workspace_id, request=request, period=period))
        .group_by(dow)
    )
    raw = db.execute(stmt).all()
    if not raw:
        return CatalogSpecialResult(
            period.label,
            "مفيش مواعيد مسجلة في الفترة والشروط المختارة.",
            ["اليوم يُحسب من وقت بداية الموعد المسجل في Linka."],
            [],
            AnalyticsCatalogChartDataRead(labels=[], series=[]),
            [],
        )
    values = {_extract_int(row.weekday): (int(row.appointments or 0), int(row.completed or 0)) for row in raw}
    rows: list[AnalyticsBIResultRow] = []
    for code, label in _WEEKDAYS:
        appointments, completed = values.get(code, (0, 0))
        rows.append(
            AnalyticsBIResultRow(
                key=f"weekday:{code}",
                label=label,
                metrics=[
                    _metric("appointments", "المواعيد", appointments),
                    _metric("completed_appointments", "الجلسات المكتملة", completed),
                ],
            )
        )
    ranked = sorted(rows, key=lambda row: int(row.metrics[0].value), reverse=True)
    total = sum(int(row.metrics[0].value) for row in rows)
    busiest = ranked[0]
    return CatalogSpecialResult(
        period.label,
        f"أكثر يوم ازدحامًا هو {busiest.label} بعدد {busiest.metrics[0].value} موعد.",
        ["اليوم يُحسب من وقت بداية الموعد المسجل في Linka.", "المواعيد المعاد جدولتها لا تدخل في الحجم التشغيلي."],
        ranked,
        AnalyticsCatalogChartDataRead(
            labels=[row.label for row in ranked],
            series=[
                AnalyticsCatalogChartSeriesRead(
                    key="appointments", label="المواعيد", format="number", values=[int(row.metrics[0].value) for row in ranked]
                )
            ],
        ),
        [_metric("appointments", "إجمالي المواعيد", total)],
    )


def _time_grid_rows(
    raw: list[Any], *, value_kind: str
) -> tuple[list[AnalyticsBIResultRow], AnalyticsCatalogChartDataRead]:
    # The query is already aggregated to at most 7 x 24 rows. Pivoting this
    # bounded result in Python avoids reading appointment records into memory.
    grid: dict[tuple[int, int], float] = {}
    present_hours: set[int] = set()
    for row in raw:
        weekday = _extract_int(row.weekday)
        hour = _extract_int(row.hour)
        present_hours.add(hour)
        if value_kind == "appointments":
            value = float(row.appointments or 0)
        elif value_kind == "no_show_rate":
            completed = int(row.completed or 0)
            no_show = int(row.no_show or 0)
            value = _pct(no_show, completed + no_show)
        elif value_kind == "cancellation_rate":
            value = _pct(int(row.cancelled or 0), int(row.appointments or 0))
        else:
            raise AnalyticsBIError("Unsupported time-grid value kind.")
        grid[(weekday, hour)] = value

    hours = sorted(present_hours)
    if not hours:
        return [], AnalyticsCatalogChartDataRead(labels=[], series=[])
    {
        "appointments": "المواعيد",
        "no_show_rate": "نسبة عدم الحضور",
        "cancellation_rate": "نسبة الإلغاء",
    }[value_kind]
    series_format = "percent" if value_kind.endswith("_rate") else "number"
    chart_series: list[AnalyticsCatalogChartSeriesRead] = []
    for weekday, label in _WEEKDAYS:
        chart_series.append(
            AnalyticsCatalogChartSeriesRead(
                key=f"weekday_{weekday}",
                label=label,
                format=series_format,
                values=[grid.get((weekday, hour), 0.0) for hour in hours],
            )
        )
    rows = [
        AnalyticsBIResultRow(
            key=f"hour:{hour}",
            label=f"{hour:02d}:00",
            metrics=[
                AnalyticsBIMetricRead(
                    key=f"{value_kind}_weekday_{weekday}",
                    label=weekday_label,
                    value=grid.get((weekday, hour), 0.0),
                )
                for weekday, weekday_label in _WEEKDAYS
            ],
        )
        for hour in hours
    ]
    return rows, AnalyticsCatalogChartDataRead(
        labels=[f"{hour:02d}:00" for hour in hours],
        series=chart_series,
    )


def _time_grid(
    db: Session,
    *,
    workspace_id: UUID,
    request: AnalyticsCatalogRunRequest,
    period: _Period,
    value_kind: str,
) -> CatalogSpecialResult:
    dow = extract("dow", Appointment.start_at).label("weekday")
    hour = extract("hour", Appointment.start_at).label("hour")
    stmt = (
        select(
            dow,
            hour,
            func.count(Appointment.id).label("appointments"),
            func.sum(case((Appointment.status == "completed", 1), else_=0)).label("completed"),
            func.sum(case((Appointment.status == "no_show", 1), else_=0)).label("no_show"),
            func.sum(case((Appointment.status == "cancelled", 1), else_=0)).label("cancelled"),
        )
        .where(*_appointment_filters(workspace_id=workspace_id, request=request, period=period))
        .group_by(dow, hour)
    )
    raw = db.execute(stmt).all()
    rows, chart_data = _time_grid_rows(raw, value_kind=value_kind)
    if not rows:
        return CatalogSpecialResult(
            period.label,
            "مفيش مواعيد مسجلة في الفترة والشروط المختارة.",
            ["الخريطة تستخدم وقت بداية الموعد المسجل في Linka."],
            [],
            chart_data,
            [],
        )
    if value_kind == "appointments":
        best = max(raw, key=lambda row: int(row.appointments or 0))
        best_label = f"{_WEEKDAY_LABELS.get(_extract_int(best.weekday), '—')} {_extract_int(best.hour):02d}:00"
        answer = f"أكثر وقت ازدحامًا هو {best_label} بعدد {int(best.appointments or 0)} موعد."
        highlights = [_metric("appointments", "إجمالي المواعيد", sum(int(row.appointments or 0) for row in raw))]
        definitions = [
            "الخريطة تجمع المواعيد حسب يوم الأسبوع وساعة بداية الموعد.",
            "كل خلية مبنية على query تجميعي؛ لا يتم تحميل سجل المواعيد كاملًا إلى التطبيق.",
        ]
    elif value_kind == "no_show_rate":
        scored = [row for row in raw if int(row.no_show or 0) > 0]
        if scored:
            best = max(scored, key=lambda row: _pct(int(row.no_show or 0), int(row.completed or 0) + int(row.no_show or 0)))
            best_rate = _pct(int(best.no_show or 0), int(best.completed or 0) + int(best.no_show or 0))
            answer = f"أعلى نسبة عدم حضور مسجلة كانت {_WEEKDAY_LABELS.get(_extract_int(best.weekday), '—')} {_extract_int(best.hour):02d}:00 بنسبة {best_rate}%."
        else:
            answer = "لا توجد جلسات مكتملة أو حالات عدم حضور كافية لحساب النسبة في الفترة المختارة."
        highlights = []
        definitions = ["نسبة عدم الحضور = no_show ÷ (completed + no_show) لكل يوم وساعة."]
    else:
        scored = [row for row in raw if int(row.cancelled or 0) > 0]
        if scored:
            best = max(scored, key=lambda row: _pct(int(row.cancelled or 0), int(row.appointments or 0)))
            best_rate = _pct(int(best.cancelled or 0), int(best.appointments or 0))
            answer = f"أعلى نسبة إلغاء مسجلة كانت {_WEEKDAY_LABELS.get(_extract_int(best.weekday), '—')} {_extract_int(best.hour):02d}:00 بنسبة {best_rate}%."
        else:
            answer = "لا توجد مواعيد ملغاة في الفترة والشروط المختارة."
        highlights = []
        definitions = ["نسبة الإلغاء = cancelled ÷ كل المواعيد غير rescheduled لكل يوم وساعة."]
    return CatalogSpecialResult(period.label, answer, definitions, rows, chart_data, highlights)


def _visit_counts_subquery(
    *, workspace_id: UUID, request: AnalyticsCatalogRunRequest, period: _Period, group_dimension: str | None = None
):
    columns: list[Any] = []
    group_columns: list[Any] = []
    if group_dimension == "service":
        columns.append(Appointment.service_id.label("group_id"))
        group_columns.append(Appointment.service_id)
    elif group_dimension == "branch":
        columns.append(Appointment.branch_id.label("group_id"))
        group_columns.append(Appointment.branch_id)
    elif group_dimension == "doctor":
        columns.append(Appointment.doctor_id.label("group_id"))
        group_columns.append(Appointment.doctor_id)
    return (
        select(
            *columns,
            Appointment.patient_id.label("patient_id"),
            func.count(Appointment.id).label("visits"),
        )
        .where(*_appointment_filters(workspace_id=workspace_id, request=request, period=period, completed_only=True))
        .group_by(*group_columns, Appointment.patient_id)
        .subquery()
    )


def _visit_conversion(
    db: Session,
    *,
    workspace_id: UUID,
    request: AnalyticsCatalogRunRequest,
    period: _Period,
    target_visit: int,
) -> CatalogSpecialResult:
    per_patient = _visit_counts_subquery(workspace_id=workspace_id, request=request, period=period)
    row = db.execute(
        select(
            func.count().label("patients"),
            func.coalesce(func.sum(case((per_patient.c.visits >= target_visit, 1), else_=0)), 0).label("converted"),
        ).select_from(per_patient)
    ).one()
    patients = int(row.patients or 0)
    converted = int(row.converted or 0)
    rate = _pct(converted, patients)
    ordinal = "الثانية" if target_visit == 2 else "الثالثة"
    metric_key = "second_visit_conversion_rate" if target_visit == 2 else "third_visit_conversion_rate"
    converted_key = "patients_with_second_visit" if target_visit == 2 else "patients_with_third_visit"
    rows = [] if patients == 0 else [
        AnalyticsBIResultRow(
            key="total",
            label="العملاء",
            metrics=[
                _metric("patients_with_completed_visit", "لديهم زيارة مكتملة", patients),
                _metric(converted_key, f"وصلوا للزيارة {ordinal}", converted),
                _metric(metric_key, f"التحويل للزيارة {ordinal}", rate),
            ],
        )
    ]
    return CatalogSpecialResult(
        period.label,
        f"{rate}% من العملاء الذين لديهم زيارة مكتملة داخل الفترة وصلوا إلى الزيارة {ordinal} داخل نفس نطاق التحليل." if patients else "مفيش زيارات مكتملة مطابقة للشروط المختارة.",
        [
            f"التحويل للزيارة {ordinal} = العملاء الذين لديهم {target_visit}+ زيارات مكتملة ÷ العملاء الذين لديهم زيارة مكتملة واحدة على الأقل داخل الفترة والشروط نفسها.",
            "الحساب يعتمد على patient_id canonical وليس اسم العميل.",
        ],
        rows,
        AnalyticsCatalogChartDataRead(labels=[], series=[]),
        rows[0].metrics if rows else [],
    )


def _label_map(db: Session, *, workspace_id: UUID, dimension: str) -> dict[str, str]:
    if dimension == "service":
        rows = db.execute(select(Service.id, Service.name).where(Service.workspace_id == workspace_id)).all()
        return {str(row.id): row.name for row in rows}
    if dimension == "branch":
        rows = db.execute(select(Branch.id, Branch.name).where(Branch.workspace_id == workspace_id)).all()
        return {str(row.id): row.name for row in rows}
    if dimension == "doctor":
        rows = db.execute(
            select(Doctor.id, Staff.first_name, Staff.last_name)
            .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
            .where(Doctor.workspace_id == workspace_id)
        ).all()
        return {
            str(row.id): " ".join(part for part in (row.first_name, row.last_name) if part).strip() or "Doctor"
            for row in rows
        }
    return {}


def _days_between_expr(db: Session, second_col: Any, first_col: Any) -> Any:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return func.julianday(second_col) - func.julianday(first_col)
    if dialect == "postgresql":
        return extract("epoch", second_col - first_col) / 86400.0
    raise AnalyticsBIError(f"Time-to-return analytics are not implemented for database dialect: {dialect}.")


def _time_to_return(
    db: Session,
    *,
    workspace_id: UUID,
    request: AnalyticsCatalogRunRequest,
    period: _Period,
    group_dimension: str | None,
) -> CatalogSpecialResult:
    group_expr: Any | None = None
    if group_dimension == "service":
        group_expr = Appointment.service_id
    elif group_dimension == "branch":
        group_expr = Appointment.branch_id
    elif group_dimension == "doctor":
        group_expr = Appointment.doctor_id

    select_group = [group_expr.label("group_id")] if group_expr is not None else []
    partition = ([group_expr] if group_expr is not None else []) + [Appointment.patient_id]
    ordered = (
        select(
            *select_group,
            Appointment.patient_id.label("patient_id"),
            Appointment.start_at.label("visit_at"),
            func.row_number().over(partition_by=partition, order_by=(Appointment.start_at, Appointment.id)).label("visit_rank"),
        )
        .where(*_appointment_filters(workspace_id=workspace_id, request=request, period=period, completed_only=True))
        .subquery()
    )
    ordered_group = [ordered.c.group_id] if group_dimension else []
    per_patient = (
        select(
            *ordered_group,
            ordered.c.patient_id,
            func.max(case((ordered.c.visit_rank == 1, ordered.c.visit_at), else_=None)).label("first_visit"),
            func.max(case((ordered.c.visit_rank == 2, ordered.c.visit_at), else_=None)).label("second_visit"),
        )
        .group_by(*ordered_group, ordered.c.patient_id)
        .subquery()
    )

    cohort_stmt = select(
        *([per_patient.c.group_id] if group_dimension else []),
        func.count().label("patients"),
        func.coalesce(func.sum(case((per_patient.c.second_visit.is_not(None), 1), else_=0)), 0).label("returning_patients"),
    )
    if group_dimension:
        cohort_stmt = cohort_stmt.group_by(per_patient.c.group_id)
    cohort_rows = db.execute(cohort_stmt).all()
    cohort: dict[str, tuple[int, int]] = {}
    for row in cohort_rows:
        key = str(row.group_id) if group_dimension else "total"
        cohort[key] = (int(row.patients or 0), int(row.returning_patients or 0))

    days_expr = _days_between_expr(db, per_patient.c.second_visit, per_patient.c.first_visit).label("days")
    interval_select = [per_patient.c.group_id] if group_dimension else []
    intervals = (
        select(*interval_select, per_patient.c.patient_id, days_expr)
        .where(per_patient.c.second_visit.is_not(None))
        .subquery()
    )
    partition_by = [intervals.c.group_id] if group_dimension else None
    rank_kwargs: dict[str, Any] = {"order_by": (intervals.c.days, intervals.c.patient_id)}
    count_kwargs: dict[str, Any] = {}
    if partition_by:
        rank_kwargs["partition_by"] = partition_by
        count_kwargs["partition_by"] = partition_by
    ranked = (
        select(
            *([intervals.c.group_id] if group_dimension else []),
            intervals.c.days,
            func.row_number().over(**rank_kwargs).label("rn"),
            func.count().over(**count_kwargs).label("cnt"),
        )
        .subquery()
    )
    middle = func.abs(2 * ranked.c.rn - ranked.c.cnt - 1) <= 1
    stats_stmt = select(
        *([ranked.c.group_id] if group_dimension else []),
        func.avg(ranked.c.days).label("avg_days"),
        func.avg(case((middle, ranked.c.days), else_=None)).label("median_days"),
    )
    if group_dimension:
        stats_stmt = stats_stmt.group_by(ranked.c.group_id)
    stat_rows = db.execute(stats_stmt).all()
    stats: dict[str, tuple[float, float]] = {}
    for row in stat_rows:
        key = str(row.group_id) if group_dimension else "total"
        if row.avg_days is not None and row.median_days is not None:
            stats[key] = (round(float(row.avg_days), 1), round(float(row.median_days), 1))

    labels = _label_map(db, workspace_id=workspace_id, dimension=group_dimension) if group_dimension else {}
    output: list[AnalyticsBIResultRow] = []
    for key, (patients, returning) in cohort.items():
        if returning <= 0:
            continue
        avg_days, median_days = stats.get(key, (0.0, 0.0))
        output.append(
            AnalyticsBIResultRow(
                key=key,
                label=labels.get(key, key) if group_dimension else "العملاء العائدون",
                metrics=[
                    _metric("median_days_to_second_visit", "الوقت المعتاد للعودة · يوم", median_days),
                    _metric("avg_days_to_second_visit", "متوسط وقت العودة · يوم", avg_days),
                    _metric("second_visit_conversion_rate", "التحويل لزيارة ثانية", _pct(returning, patients)),
                    _metric("patients_with_second_visit", "عملاء عادوا", returning),
                ],
            )
        )
    output.sort(key=lambda row: float(row.metrics[0].value))
    if group_dimension:
        output = output[: (request.limit or 10)]
    chart = AnalyticsCatalogChartDataRead(
        labels=[row.label for row in output],
        series=[
            AnalyticsCatalogChartSeriesRead(
                key="median_days_to_second_visit",
                label="الوقت المعتاد للعودة · يوم",
                format="number",
                values=[float(row.metrics[0].value) for row in output],
            ),
            AnalyticsCatalogChartSeriesRead(
                key="avg_days_to_second_visit",
                label="متوسط وقت العودة · يوم",
                format="number",
                values=[float(row.metrics[1].value) for row in output],
            ),
        ] if output else [],
    )
    if not output:
        answer = "مفيش عملاء لديهم زيارتان مكتملتان داخل الفترة والشروط المختارة."
    elif group_dimension:
        answer = f"أسرع عودة معتادة كانت مع {output[0].label}: {output[0].metrics[0].value} يوم."
    else:
        answer = f"الوقت المعتاد للعودة للزيارة الثانية هو {output[0].metrics[0].value} يوم."
    definitions = [
        "وقت العودة يُحسب بين أول وثاني زيارة completed لنفس العميل داخل الفترة والشروط المختارة.",
        "الوقت المعتاد = median بالأيام، ويُعرض المتوسط بجانبه حتى لا تؤثر الحالات البعيدة على الرقم الأساسي.",
        "ترتيب الزيارات يعتمد على patient_id ووقت بداية الموعد، وليس الاسم.",
    ]
    highlights = output[0].metrics if output and not group_dimension else []
    return CatalogSpecialResult(period.label, answer, definitions, output, chart, highlights)


def _lapsed_rate(
    db: Session,
    *,
    workspace_id: UUID,
    request: AnalyticsCatalogRunRequest,
    period: _Period,
) -> CatalogSpecialResult:
    inactivity_days = request.inactivity_days or 180
    completed = (
        select(
            Appointment.patient_id.label("patient_id"),
            func.max(Appointment.start_at).label("last_completed_at"),
        )
        .where(*_appointment_filters(workspace_id=workspace_id, request=request, period=period, completed_only=True))
        .group_by(Appointment.patient_id)
        .subquery()
    )
    cutoff = period.end - timedelta(days=inactivity_days)
    future_exists = select(Appointment.id).where(
        Appointment.workspace_id == workspace_id,
        Appointment.patient_id == completed.c.patient_id,
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.start_at >= period.end,
    ).exists()
    row = db.execute(
        select(
            func.count().label("patients"),
            func.coalesce(
                func.sum(case(((completed.c.last_completed_at <= cutoff) & (~future_exists), 1), else_=0)),
                0,
            ).label("lapsed"),
        ).select_from(completed)
    ).one()
    patients = int(row.patients or 0)
    lapsed = int(row.lapsed or 0)
    rate = _pct(lapsed, patients)
    rows = [] if patients == 0 else [
        AnalyticsBIResultRow(
            key="total",
            label="العملاء",
            metrics=[
                _metric("patients_with_completed_visit", "لديهم زيارة مكتملة", patients),
                _metric("lapsed_patients", "عملاء منقطعون", lapsed),
                _metric("lapsed_rate", "نسبة العملاء المنقطعين", rate),
            ],
        )
    ]
    return CatalogSpecialResult(
        f"{period.label} · انقطاع {inactivity_days} يوم",
        f"نسبة العملاء المنقطعين {rate}% من العملاء الذين لديهم زيارة مكتملة مطابقة." if patients else "مفيش زيارات مكتملة مطابقة للشروط المختارة.",
        [
            f"العميل المنقطع = آخر زيارة completed مطابقة أقدم من {inactivity_days} يوم ولا يوجد له أي موعد نشط قادم في العيادة.",
            "المقام = العملاء الذين لديهم زيارة completed واحدة على الأقل ضمن الفترة والشروط المختارة.",
        ],
        rows,
        AnalyticsCatalogChartDataRead(labels=[], series=[]),
        rows[0].metrics if rows else [],
    )


def execute_special_catalog_analysis(
    db: Session,
    *,
    workspace_id: UUID,
    analysis_key: str,
    request: AnalyticsCatalogRunRequest,
    default_lookback_days: int | None,
    now: datetime | None = None,
) -> CatalogSpecialResult:
    period = _period(request, default_lookback_days=default_lookback_days, now=now)
    if analysis_key == "appointment_peak_weekdays":
        return _peak_weekdays(db, workspace_id=workspace_id, request=request, period=period)
    if analysis_key == "appointment_peak_hours":
        return _time_grid(db, workspace_id=workspace_id, request=request, period=period, value_kind="appointments")
    if analysis_key == "no_show_peak_times":
        return _time_grid(db, workspace_id=workspace_id, request=request, period=period, value_kind="no_show_rate")
    if analysis_key == "cancellation_peak_times":
        return _time_grid(db, workspace_id=workspace_id, request=request, period=period, value_kind="cancellation_rate")
    if analysis_key == "second_visit_conversion":
        return _visit_conversion(db, workspace_id=workspace_id, request=request, period=period, target_visit=2)
    if analysis_key == "third_visit_conversion":
        return _visit_conversion(db, workspace_id=workspace_id, request=request, period=period, target_visit=3)
    if analysis_key == "time_to_return":
        return _time_to_return(db, workspace_id=workspace_id, request=request, period=period, group_dimension=None)
    if analysis_key == "time_to_return_by_service":
        return _time_to_return(db, workspace_id=workspace_id, request=request, period=period, group_dimension="service")
    if analysis_key == "lapsed_rate":
        return _lapsed_rate(db, workspace_id=workspace_id, request=request, period=period)
    raise AnalyticsBIError("Unknown special analytics catalog analysis key.")
