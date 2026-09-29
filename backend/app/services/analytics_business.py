from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, distinct, extract, func, or_, select, union_all
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.patient_package import PatientPackage
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.schemas.analytics_bi import AnalyticsBIMetricRead, AnalyticsBIResultRow
from app.schemas.analytics_business import (
    MONEY_METRICS,
    RATE_METRICS,
    AnalyticsBusinessAnswerRead,
    AnalyticsBusinessPlan,
)
from app.services.analytics_bi import AnalyticsBIError


@dataclass(frozen=True)
class _Period:
    start: datetime | None
    end: datetime
    label: str


@dataclass
class _GroupValues:
    appointments: int = 0
    completed: int = 0
    no_show: int = 0
    cancelled: int = 0
    unique_patients: int = 0
    gross_paid_minor: int = 0
    refunded_minor: int = 0
    net_paid_minor: int = 0
    paying_patients: int = 0
    paid_completed_appointments: int = 0
    repeat_patients: int = 0
    repeat_denominator: int = 0
    new_patients: int = 0


GroupKey = tuple[tuple[str, tuple[str, ...]], ...]


_METRIC_LABELS: dict[str, str] = {
    "appointments": "الحجوزات",
    "completed_appointments": "مواعيد مكتملة",
    "no_show_appointments": "حالات عدم الحضور",
    "cancelled_appointments": "مواعيد ملغاة",
    "unique_patients": "عملاء فريدون",
    "attendance_rate": "نسبة الحضور",
    "no_show_rate": "نسبة عدم الحضور",
    "cancellation_rate": "نسبة الإلغاء",
    "gross_paid_minor": "إجمالي المدفوع",
    "refunded_minor": "المبالغ المرتجعة",
    "net_paid_minor": "صافي المدفوع",
    "avg_net_paid_per_paying_patient_minor": "متوسط صافي المدفوع لكل عميل دافع",
    "paying_patients": "عملاء دفعوا",
    "paid_completed_appointments": "مواعيد مكتملة لها دفع صريح",
    "completion_rate": "نسبة الإكمال",
    "paid_completion_rate": "تحويل المكتمل إلى دفع",
    "booking_to_paid_rate": "تحويل الحجز إلى مكتمل مدفوع",
    "repeat_patients": "عملاء متكررون",
    "repeat_rate": "نسبة العملاء المتكررين",
    "new_patients": "عملاء جدد",
    "same_service_repeat_rate": "العودة لنفس الخدمة",
}


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current


def _periods(plan: AnalyticsBusinessPlan, *, current: datetime) -> tuple[_Period, _Period | None]:
    tz = current.tzinfo or UTC
    if plan.lookback_days is not None:
        start = current - timedelta(days=plan.lookback_days)
        active = _Period(start=start, end=current, label=f"آخر {plan.lookback_days} يوم")
    elif plan.start_date is not None and plan.end_date is not None:
        start = datetime.combine(plan.start_date, time.min, tzinfo=tz)
        end = datetime.combine(plan.end_date + timedelta(days=1), time.min, tzinfo=tz)
        active = _Period(
            start=start,
            end=end,
            label=f"من {plan.start_date.isoformat()} إلى {plan.end_date.isoformat()}",
        )
    else:
        active = _Period(start=None, end=current, label="كل التاريخ المتاح")

    if plan.comparison != "previous_period":
        return active, None
    if active.start is None:
        raise AnalyticsBIError("Previous-period comparison requires a bounded period.")
    width = active.end - active.start
    previous = _Period(
        start=active.start - width,
        end=active.start,
        label="الفترة السابقة المساوية",
    )
    return active, previous


def _as_uuids(values: list[str]) -> list[UUID]:
    result: list[UUID] = []
    for value in values:
        try:
            result.append(UUID(value))
        except ValueError as exc:
            raise AnalyticsBIError("Business analytics plan contains an invalid canonical UUID.") from exc
    return result


def validate_business_plan_entities(
    plan: AnalyticsBusinessPlan,
    *,
    catalog: dict[str, list[dict[str, str]]],
) -> AnalyticsBusinessPlan:
    for field_name, collection_name in (
        ("service_ids", "services"),
        ("branch_ids", "branches"),
        ("doctor_ids", "doctors"),
    ):
        allowed = {str(item.get("id")) for item in catalog.get(collection_name, [])}
        if any(value not in allowed for value in getattr(plan, field_name)):
            raise AnalyticsBIError(
                f"Business analytics plan referenced an unknown canonical {collection_name[:-1]} id."
            )
    return plan


def _time_specs(dimension: str, timestamp: Any) -> list[Any]:
    if dimension == "day":
        return [extract("year", timestamp), extract("month", timestamp), extract("day", timestamp)]
    if dimension == "week":
        return [extract("year", timestamp), extract("week", timestamp)]
    if dimension == "month":
        return [extract("year", timestamp), extract("month", timestamp)]
    return []


def _group_expressions(group_by: list[str], *, timestamp: Any) -> list[Any]:
    expressions: list[Any] = []
    for dimension in group_by:
        if dimension == "service":
            expressions.append(Appointment.service_id)
        elif dimension == "branch":
            expressions.append(Appointment.branch_id)
        elif dimension == "doctor":
            expressions.append(Appointment.doctor_id)
        elif dimension == "source":
            expressions.append(Appointment.source)
        else:
            expressions.extend(_time_specs(dimension, timestamp))
    return expressions


def _package_group_expressions(group_by: list[str], *, timestamp: Any) -> list[Any]:
    """Group package-sale finance only by dimensions the sale actually knows."""
    expressions: list[Any] = []
    for dimension in group_by:
        if dimension == "service":
            expressions.append(PatientPackage.service_id)
        elif dimension in {"branch", "doctor", "source"}:
            raise AnalyticsBIError(
                f"Package purchases cannot be attributed to {dimension} without explicit purchase evidence."
            )
        else:
            expressions.extend(_time_specs(dimension, timestamp))
    return expressions


def _normalize_time_part(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def _group_key(group_by: list[str], raw_values: tuple[Any, ...]) -> GroupKey:
    offset = 0
    parts: list[tuple[str, tuple[str, ...]]] = []
    for dimension in group_by:
        width = 1 if dimension in {"service", "branch", "doctor", "source"} else (3 if dimension == "day" else 2)
        raw_part = raw_values[offset : offset + width]
        if dimension in {"service", "branch", "doctor", "source"}:
            values = tuple(str(value) if value is not None else "" for value in raw_part)
        else:
            values = tuple(_normalize_time_part(value) for value in raw_part)
        offset += width
        parts.append((dimension, values))
    return tuple(parts)


def _appointment_filters(
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
) -> list[Any]:
    filters: list[Any] = [Appointment.workspace_id == workspace_id, Appointment.status != "rescheduled"]
    if period.start is not None:
        filters.append(Appointment.start_at >= period.start)
    filters.append(Appointment.start_at < period.end)
    if plan.service_ids:
        filters.append(Appointment.service_id.in_(_as_uuids(plan.service_ids)))
    if plan.branch_ids:
        filters.append(Appointment.branch_id.in_(_as_uuids(plan.branch_ids)))
    if plan.doctor_ids:
        filters.append(Appointment.doctor_id.in_(_as_uuids(plan.doctor_ids)))
    return filters


def _payment_filters(
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
) -> list[Any]:
    filters: list[Any] = [PaymentTransaction.workspace_id == workspace_id]
    if period.start is not None:
        filters.append(PaymentTransaction.created_at >= period.start)
    filters.append(PaymentTransaction.created_at < period.end)
    if plan.currency:
        filters.append(PaymentTransaction.currency == plan.currency)
    return filters


def _ensure_group(groups: dict[GroupKey, _GroupValues], key: GroupKey) -> _GroupValues:
    if key not in groups:
        groups[key] = _GroupValues()
    return groups[key]


def _collect_appointment_metrics(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
    groups: dict[GroupKey, _GroupValues],
) -> None:
    needed = {
        "appointments",
        "completed_appointments",
        "no_show_appointments",
        "cancelled_appointments",
        "unique_patients",
        "attendance_rate",
        "no_show_rate",
        "cancellation_rate",
        "completion_rate",
        "paid_completion_rate",
        "booking_to_paid_rate",
    }.intersection(plan.metrics)
    if not needed:
        return
    group_exprs = _group_expressions(plan.group_by, timestamp=Appointment.start_at)
    stmt = select(
        *group_exprs,
        func.count(Appointment.id).label("appointments"),
        func.sum(case((Appointment.status == "completed", 1), else_=0)).label("completed"),
        func.sum(case((Appointment.status == "no_show", 1), else_=0)).label("no_show"),
        func.sum(case((Appointment.status == "cancelled", 1), else_=0)).label("cancelled"),
        func.count(distinct(Appointment.patient_id)).label("unique_patients"),
    ).where(*_appointment_filters(workspace_id=workspace_id, plan=plan, period=period))
    if group_exprs:
        stmt = stmt.group_by(*group_exprs)
    for row in db.execute(stmt).all():
        raw = tuple(row[: len(group_exprs)])
        key = _group_key(plan.group_by, raw)
        values = _ensure_group(groups, key)
        values.appointments = int(row.appointments or 0)
        values.completed = int(row.completed or 0)
        values.no_show = int(row.no_show or 0)
        values.cancelled = int(row.cancelled or 0)
        values.unique_patients = int(row.unique_patients or 0)


def _collect_payment_metrics(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
    groups: dict[GroupKey, _GroupValues],
) -> None:
    needed = {"gross_paid_minor", "refunded_minor", "net_paid_minor", "avg_net_paid_per_paying_patient_minor", "paying_patients"}.intersection(plan.metrics)
    if not needed:
        return
    entity_scoped = bool(
        plan.service_ids
        or plan.branch_ids
        or plan.doctor_ids
        or any(dim in {"service", "branch", "doctor", "source"} for dim in plan.group_by)
    )
    signed = case(
        (PaymentTransaction.transaction_type == "payment", PaymentTransaction.amount_minor),
        else_=-PaymentTransaction.amount_minor,
    )
    gross = case(
        (PaymentTransaction.transaction_type == "payment", PaymentTransaction.amount_minor),
        else_=0,
    )
    refunded = case(
        (PaymentTransaction.transaction_type == "refund", PaymentTransaction.amount_minor),
        else_=0,
    )

    if not entity_scoped:
        group_exprs: list[Any] = []
        for dimension in plan.group_by:
            group_exprs.extend(_time_specs(dimension, PaymentTransaction.created_at))
        stmt = select(
            *group_exprs,
            func.coalesce(func.sum(gross), 0).label("gross"),
            func.coalesce(func.sum(refunded), 0).label("refunded"),
            func.coalesce(func.sum(signed), 0).label("net"),
            func.count(distinct(case((PaymentTransaction.transaction_type == "payment", PaymentTransaction.patient_id)))).label(
                "paying_patients"
            ),
        ).where(*_payment_filters(workspace_id=workspace_id, plan=plan, period=period))
        if group_exprs:
            stmt = stmt.group_by(*group_exprs)
        for row in db.execute(stmt).all():
            raw = tuple(row[: len(group_exprs)])
            key = _group_key(plan.group_by, raw)
            values = _ensure_group(groups, key)
            values.gross_paid_minor = int(row.gross or 0)
            values.refunded_minor = int(row.refunded or 0)
            values.net_paid_minor = int(row.net or 0)
            values.paying_patients = int(row.paying_patients or 0)
        return

    # Entity-attributed appointment finance uses explicit allocations only.
    # A package purchase is a separate service-level commercial fact and is
    # deliberately excluded here even if legacy data also attached an
    # appointment allocation, preventing the package sale from being counted
    # once per session or attributed to a doctor/branch without purchase facts.
    package_purchase_ids = select(PatientPackage.purchase_transaction_id).where(
        PatientPackage.workspace_id == workspace_id,
        PatientPackage.purchase_transaction_id.is_not(None),
    )
    not_package_finance = and_(
        PaymentTransaction.patient_package_id.is_(None),
        ~PaymentTransaction.id.in_(package_purchase_ids),
        or_(
            PaymentTransaction.reference_transaction_id.is_(None),
            ~PaymentTransaction.reference_transaction_id.in_(package_purchase_ids),
        ),
    )
    allocation_signed = case(
        (PaymentTransaction.transaction_type == "payment", PaymentAllocation.amount_minor),
        else_=-PaymentAllocation.amount_minor,
    )
    allocation_gross = case(
        (PaymentTransaction.transaction_type == "payment", PaymentAllocation.amount_minor),
        else_=0,
    )
    allocation_refunded = case(
        (PaymentTransaction.transaction_type == "refund", PaymentAllocation.amount_minor),
        else_=0,
    )
    group_exprs = _group_expressions(plan.group_by, timestamp=PaymentTransaction.created_at)
    appointment_filters: list[Any] = [Appointment.workspace_id == workspace_id]
    if plan.service_ids:
        appointment_filters.append(Appointment.service_id.in_(_as_uuids(plan.service_ids)))
    if plan.branch_ids:
        appointment_filters.append(Appointment.branch_id.in_(_as_uuids(plan.branch_ids)))
    if plan.doctor_ids:
        appointment_filters.append(Appointment.doctor_id.in_(_as_uuids(plan.doctor_ids)))
    allocation_stmt = (
        select(
            *group_exprs,
            func.coalesce(func.sum(allocation_gross), 0).label("gross"),
            func.coalesce(func.sum(allocation_refunded), 0).label("refunded"),
            func.coalesce(func.sum(allocation_signed), 0).label("net"),
        )
        .select_from(PaymentTransaction)
        .join(
            PaymentAllocation,
            (PaymentAllocation.workspace_id == PaymentTransaction.workspace_id)
            & (PaymentAllocation.transaction_id == PaymentTransaction.id),
        )
        .join(
            Appointment,
            (Appointment.workspace_id == PaymentAllocation.workspace_id)
            & (Appointment.id == PaymentAllocation.appointment_id),
        )
        .where(
            *_payment_filters(workspace_id=workspace_id, plan=plan, period=period),
            *appointment_filters,
            not_package_finance,
        )
    )
    if group_exprs:
        allocation_stmt = allocation_stmt.group_by(*group_exprs)
    for row in db.execute(allocation_stmt).all():
        raw = tuple(row[: len(group_exprs)])
        key = _group_key(plan.group_by, raw)
        values = _ensure_group(groups, key)
        values.gross_paid_minor += int(row.gross or 0)
        values.refunded_minor += int(row.refunded or 0)
        values.net_paid_minor += int(row.net or 0)

    # Package sales carry explicit service attribution, but no doctor/branch/source
    # attribution. Include them only when the requested entity scope can be
    # answered from the package purchase itself. Refunds follow the original
    # package purchase back to the same service.
    package_service_attributable = (
        not plan.branch_ids
        and not plan.doctor_ids
        and not any(dim in {"branch", "doctor", "source"} for dim in plan.group_by)
        and (bool(plan.service_ids) or "service" in plan.group_by)
    )
    if package_service_attributable:
        package_group_exprs = _package_group_expressions(
            plan.group_by, timestamp=PaymentTransaction.created_at
        )
        package_join = and_(
            PatientPackage.workspace_id == PaymentTransaction.workspace_id,
            or_(
                PaymentTransaction.patient_package_id == PatientPackage.id,
                and_(
                    PaymentTransaction.transaction_type == "payment",
                    PatientPackage.purchase_transaction_id == PaymentTransaction.id,
                ),
                and_(
                    PaymentTransaction.transaction_type == "refund",
                    PatientPackage.purchase_transaction_id == PaymentTransaction.reference_transaction_id,
                ),
            ),
        )
        package_filters: list[Any] = [PatientPackage.workspace_id == workspace_id]
        if plan.service_ids:
            package_filters.append(PatientPackage.service_id.in_(_as_uuids(plan.service_ids)))
        package_stmt = (
            select(
                *package_group_exprs,
                func.coalesce(func.sum(gross), 0).label("gross"),
                func.coalesce(func.sum(refunded), 0).label("refunded"),
                func.coalesce(func.sum(signed), 0).label("net"),
            )
            .select_from(PaymentTransaction)
            .join(PatientPackage, package_join)
            .where(
                *_payment_filters(workspace_id=workspace_id, plan=plan, period=period),
                *package_filters,
            )
        )
        if package_group_exprs:
            package_stmt = package_stmt.group_by(*package_group_exprs)
        for row in db.execute(package_stmt).all():
            raw = tuple(row[: len(package_group_exprs)])
            key = _group_key(plan.group_by, raw)
            values = _ensure_group(groups, key)
            values.gross_paid_minor += int(row.gross or 0)
            values.refunded_minor += int(row.refunded or 0)
            values.net_paid_minor += int(row.net or 0)

    # Paying-patient counts must be distinct across direct appointment payments
    # and package purchases. Build one union and count once per group so a patient
    # who paid both ways is never double-counted.
    if "paying_patients" in needed or "avg_net_paid_per_paying_patient_minor" in needed:
        payer_group_exprs = _group_expressions(plan.group_by, timestamp=PaymentTransaction.created_at)
        allocation_payers = (
            select(
                *[expr.label(f"g{index}") for index, expr in enumerate(payer_group_exprs)],
                PaymentTransaction.patient_id.label("patient_id"),
            )
            .select_from(PaymentTransaction)
            .join(
                PaymentAllocation,
                (PaymentAllocation.workspace_id == PaymentTransaction.workspace_id)
                & (PaymentAllocation.transaction_id == PaymentTransaction.id),
            )
            .join(
                Appointment,
                (Appointment.workspace_id == PaymentAllocation.workspace_id)
                & (Appointment.id == PaymentAllocation.appointment_id),
            )
            .where(
                *_payment_filters(workspace_id=workspace_id, plan=plan, period=period),
                *appointment_filters,
                PaymentTransaction.transaction_type == "payment",
                not_package_finance,
            )
            .distinct()
        )
        payer_selects = [allocation_payers]
        if package_service_attributable:
            package_payer_group_exprs = _package_group_expressions(
                plan.group_by, timestamp=PaymentTransaction.created_at
            )
            package_payers = (
                select(
                    *[expr.label(f"g{index}") for index, expr in enumerate(package_payer_group_exprs)],
                    PaymentTransaction.patient_id.label("patient_id"),
                )
                .select_from(PaymentTransaction)
                .join(
                    PatientPackage,
                    and_(
                        PatientPackage.workspace_id == PaymentTransaction.workspace_id,
                        or_(
                            PaymentTransaction.patient_package_id == PatientPackage.id,
                            PatientPackage.purchase_transaction_id == PaymentTransaction.id,
                        ),
                    ),
                )
                .where(
                    *_payment_filters(workspace_id=workspace_id, plan=plan, period=period),
                    *package_filters,
                    PaymentTransaction.transaction_type == "payment",
                )
                .distinct()
            )
            payer_selects.append(package_payers)
        payer_union = union_all(*payer_selects).subquery()
        payer_group_cols = [payer_union.c[f"g{index}"] for index in range(len(payer_group_exprs))]
        payer_stmt = select(
            *payer_group_cols,
            func.count(distinct(payer_union.c.patient_id)).label("paying_patients"),
        )
        if payer_group_cols:
            payer_stmt = payer_stmt.group_by(*payer_group_cols)
        for row in db.execute(payer_stmt).all():
            raw = tuple(row[: len(payer_group_cols)])
            key = _group_key(plan.group_by, raw)
            _ensure_group(groups, key).paying_patients = int(row.paying_patients or 0)


def _collect_paid_completed_metrics(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
    groups: dict[GroupKey, _GroupValues],
) -> None:
    needed = {"paid_completed_appointments", "paid_completion_rate", "booking_to_paid_rate"}.intersection(plan.metrics)
    if not needed:
        return
    group_exprs = _group_expressions(plan.group_by, timestamp=Appointment.start_at)
    stmt = (
        select(*group_exprs, func.count(distinct(Appointment.id)).label("paid_completed"))
        .join(
            PaymentAllocation,
            (PaymentAllocation.workspace_id == Appointment.workspace_id)
            & (PaymentAllocation.appointment_id == Appointment.id),
        )
        .join(
            PaymentTransaction,
            (PaymentTransaction.workspace_id == PaymentAllocation.workspace_id)
            & (PaymentTransaction.id == PaymentAllocation.transaction_id),
        )
        .where(
            *_appointment_filters(workspace_id=workspace_id, plan=plan, period=period),
            Appointment.status == "completed",
            PaymentTransaction.transaction_type == "payment",
        )
    )
    if group_exprs:
        stmt = stmt.group_by(*group_exprs)
    for row in db.execute(stmt).all():
        raw = tuple(row[: len(group_exprs)])
        key = _group_key(plan.group_by, raw)
        _ensure_group(groups, key).paid_completed_appointments = int(row.paid_completed or 0)


def _collect_repeat_metrics(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
    groups: dict[GroupKey, _GroupValues],
) -> None:
    needed = {"repeat_patients", "repeat_rate", "same_service_repeat_rate"}.intersection(plan.metrics)
    if not needed:
        return
    entity_dims = [dim for dim in plan.group_by if dim in {"service", "branch", "doctor", "source"}]
    group_exprs = _group_expressions(entity_dims, timestamp=Appointment.start_at)
    per_patient = (
        select(
            *group_exprs,
            Appointment.patient_id.label("patient_id"),
            func.count(Appointment.id).label("visits"),
        )
        .where(
            *_appointment_filters(workspace_id=workspace_id, plan=plan, period=period),
            Appointment.status == "completed",
        )
        .group_by(*group_exprs, Appointment.patient_id)
        .subquery()
    )
    sub_group_columns = [per_patient.c[index] for index in range(len(group_exprs))]
    stmt = select(
        *sub_group_columns,
        func.count().label("denominator"),
        func.sum(case((per_patient.c.visits >= 2, 1), else_=0)).label("repeat_patients"),
    )
    if sub_group_columns:
        stmt = stmt.group_by(*sub_group_columns)
    for row in db.execute(stmt).all():
        raw = tuple(row[: len(sub_group_columns)])
        key = _group_key(entity_dims, raw)
        values = _ensure_group(groups, key)
        values.repeat_denominator = int(row.denominator or 0)
        values.repeat_patients = int(row.repeat_patients or 0)


def _collect_new_patient_metrics(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
    groups: dict[GroupKey, _GroupValues],
) -> None:
    if "new_patients" not in plan.metrics:
        return
    first_seen = func.coalesce(Patient.source_created_at, Patient.created_at)
    group_exprs: list[Any] = []
    for dimension in plan.group_by:
        group_exprs.extend(_time_specs(dimension, first_seen))
    filters: list[Any] = [Patient.workspace_id == workspace_id]
    if period.start is not None:
        filters.append(first_seen >= period.start)
    filters.append(first_seen < period.end)
    stmt = select(*group_exprs, func.count(Patient.id).label("new_patients")).where(*filters)
    if group_exprs:
        stmt = stmt.group_by(*group_exprs)
    for row in db.execute(stmt).all():
        raw = tuple(row[: len(group_exprs)])
        key = _group_key(plan.group_by, raw)
        _ensure_group(groups, key).new_patients = int(row.new_patients or 0)


def _metric_value(metric: str, values: _GroupValues) -> int | float:
    if metric == "appointments":
        return values.appointments
    if metric == "completed_appointments":
        return values.completed
    if metric == "no_show_appointments":
        return values.no_show
    if metric == "cancelled_appointments":
        return values.cancelled
    if metric == "unique_patients":
        return values.unique_patients
    if metric == "gross_paid_minor":
        return values.gross_paid_minor
    if metric == "refunded_minor":
        return values.refunded_minor
    if metric == "net_paid_minor":
        return values.net_paid_minor
    if metric == "avg_net_paid_per_paying_patient_minor":
        return round(values.net_paid_minor / values.paying_patients) if values.paying_patients else 0
    if metric == "paying_patients":
        return values.paying_patients
    if metric == "paid_completed_appointments":
        return values.paid_completed_appointments
    if metric == "repeat_patients":
        return values.repeat_patients
    if metric == "new_patients":
        return values.new_patients
    if metric == "attendance_rate":
        denominator = values.completed + values.no_show
        return round((values.completed / denominator) * 100.0, 1) if denominator else 0.0
    if metric == "no_show_rate":
        denominator = values.completed + values.no_show
        return round((values.no_show / denominator) * 100.0, 1) if denominator else 0.0
    if metric == "cancellation_rate":
        return round((values.cancelled / values.appointments) * 100.0, 1) if values.appointments else 0.0
    if metric == "completion_rate":
        return round((values.completed / values.appointments) * 100.0, 1) if values.appointments else 0.0
    if metric == "paid_completion_rate":
        return (
            round((values.paid_completed_appointments / values.completed) * 100.0, 1)
            if values.completed
            else 0.0
        )
    if metric == "booking_to_paid_rate":
        return (
            round((values.paid_completed_appointments / values.appointments) * 100.0, 1)
            if values.appointments
            else 0.0
        )
    if metric in {"repeat_rate", "same_service_repeat_rate"}:
        return (
            round((values.repeat_patients / values.repeat_denominator) * 100.0, 1)
            if values.repeat_denominator
            else 0.0
        )
    raise AnalyticsBIError(f"Unsupported business metric: {metric}")


def _load_entity_labels(db: Session, *, workspace_id: UUID) -> dict[str, dict[str, str]]:
    services = db.execute(
        select(Service.id, Service.name).where(Service.workspace_id == workspace_id)
    ).all()
    branches = db.execute(
        select(Branch.id, Branch.name).where(Branch.workspace_id == workspace_id)
    ).all()
    doctors = db.execute(
        select(Doctor.id, Staff.first_name, Staff.last_name)
        .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
        .where(Doctor.workspace_id == workspace_id)
    ).all()
    return {
        "service": {str(row.id): row.name for row in services},
        "branch": {str(row.id): row.name for row in branches},
        "doctor": {
            str(row.id): " ".join(part for part in (row.first_name, row.last_name) if part).strip() or "Doctor"
            for row in doctors
        },
    }


def _dimension_label(dimension: str, values: tuple[str, ...], labels: dict[str, dict[str, str]]) -> str:
    if dimension in {"service", "branch", "doctor"}:
        raw = values[0] if values else ""
        try:
            canonical = str(UUID(raw))
        except (ValueError, TypeError):
            canonical = raw
        return labels.get(dimension, {}).get(canonical, canonical or "—")
    if dimension == "source":
        return values[0] if values and values[0] else "unknown"
    if dimension == "day":
        return f"{int(values[0]):04d}-{int(values[1]):02d}-{int(values[2]):02d}"
    if dimension == "week":
        return f"{int(values[0]):04d}-W{int(values[1]):02d}"
    if dimension == "month":
        return f"{int(values[0]):04d}-{int(values[1]):02d}"
    return "—"


def _group_label(key: GroupKey, labels: dict[str, dict[str, str]]) -> str:
    if not key:
        return "الإجمالي"
    return " · ".join(_dimension_label(dim, values, labels) for dim, values in key)


def _group_token(key: GroupKey) -> str:
    if not key:
        return "total"
    return "|".join(f"{dim}:{','.join(values)}" for dim, values in key)


def _metric_read(metric: str, value: int | float, *, currency: str | None) -> AnalyticsBIMetricRead:
    return AnalyticsBIMetricRead(
        key=metric,
        label=_METRIC_LABELS[metric],
        value=value,
        currency=currency if metric in MONEY_METRICS else None,
    )


def _comparison_metrics(
    *,
    metric: str,
    current: int | float,
    previous: int | float,
    currency: str | None,
) -> list[AnalyticsBIMetricRead]:
    result = [
        _metric_read(metric, current, currency=currency),
        AnalyticsBIMetricRead(
            key=f"{metric}_previous",
            label=f"{_METRIC_LABELS[metric]} · الفترة السابقة",
            value=previous,
            currency=currency if metric in MONEY_METRICS else None,
        ),
    ]
    if metric in RATE_METRICS:
        result.append(
            AnalyticsBIMetricRead(
                key=f"{metric}_delta_points",
                label="التغير · نقطة مئوية",
                value=round(float(current) - float(previous), 1),
            )
        )
    else:
        delta: int | float | str
        if float(previous) == 0.0:
            delta = "—"
        else:
            delta = round(((float(current) - float(previous)) / abs(float(previous))) * 100.0, 1)
        result.append(
            AnalyticsBIMetricRead(
                key=f"{metric}_change_percent",
                label="التغير %",
                value=delta,
            )
        )
    return result


def _execute_period(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBusinessPlan,
    period: _Period,
) -> dict[GroupKey, _GroupValues]:
    groups: dict[GroupKey, _GroupValues] = {}
    _collect_appointment_metrics(db, workspace_id=workspace_id, plan=plan, period=period, groups=groups)
    _collect_payment_metrics(db, workspace_id=workspace_id, plan=plan, period=period, groups=groups)
    _collect_paid_completed_metrics(db, workspace_id=workspace_id, plan=plan, period=period, groups=groups)
    _collect_repeat_metrics(db, workspace_id=workspace_id, plan=plan, period=period, groups=groups)
    _collect_new_patient_metrics(db, workspace_id=workspace_id, plan=plan, period=period, groups=groups)
    if not groups and not plan.group_by:
        groups[tuple()] = _GroupValues()
    return groups


def _definitions(plan: AnalyticsBusinessPlan) -> list[str]:
    definitions = [
        "الخطة يختارها الـAI من metrics/dimensions مسموحة فقط، والتنفيذ يتم بqueries ثابتة على canonical Linka data بدون SQL مولّد.",
        "كل filters على service/branch/doctor تستخدم canonical IDs داخل workspace الحالية فقط.",
    ]
    if any(metric in MONEY_METRICS for metric in plan.metrics):
        if plan.service_ids or plan.branch_ids or plan.doctor_ids or any(
            dim in {"service", "branch", "doctor", "source"} for dim in plan.group_by
        ):
            definitions.append(
                "Revenue المنسوب لخدمة يجمع payment allocations الصريحة للمواعيد + شراء الباقات المرتبط صراحةً بالخدمة؛ Revenue الفرع/الدكتور/source يظل allocation-only ولا يتم تخمين attribution غير موجود."
            )
        else:
            definitions.append("الأرقام المالية = payment transactions الفعلية في العملة المحددة، وnet = payments ناقص refunds.")
    if any(metric in {"paid_completed_appointments", "paid_completion_rate", "booking_to_paid_rate"} for metric in plan.metrics):
        definitions.append(
            "الموعد المكتمل المدفوع = completed appointment لديه explicit payment allocation من transaction من نوع payment؛ ده funnel تشغيلي وليس تقرير تسوية محاسبي."
        )
    if any(metric in {"repeat_patients", "repeat_rate"} for metric in plan.metrics):
        definitions.append("العميل المتكرر = مريض لديه زيارتان completed أو أكثر داخل نفس نطاق التحليل/التقسيم.")
    if "same_service_repeat_rate" in plan.metrics:
        definitions.append("Same-service retention = المرضى الذين لديهم 2+ completed visits لنفس الخدمة ÷ المرضى الذين لديهم completed visit واحدة على الأقل لها.")
    if "new_patients" in plan.metrics:
        definitions.append("العميل الجديد يُنسب إلى source_created_at عند توفره، وإلا created_at داخل Linka.")
    if plan.comparison == "previous_period":
        definitions.append("المقارنة تستخدم الفترة السابقة المساوية تمامًا في الطول؛ تغير الـrates يظهر كنقاط مئوية.")
    return definitions


def execute_business_plan(
    db: Session,
    *,
    workspace_id: UUID,
    question: str,
    plan: AnalyticsBusinessPlan,
    model: str | None = None,
    now: datetime | None = None,
) -> AnalyticsBusinessAnswerRead:
    current = _now(now)
    active_period, previous_period = _periods(plan, current=current)
    current_groups = _execute_period(
        db,
        workspace_id=workspace_id,
        plan=plan,
        period=active_period,
    )
    previous_groups = (
        _execute_period(db, workspace_id=workspace_id, plan=plan, period=previous_period)
        if previous_period is not None
        else {}
    )
    labels = _load_entity_labels(db, workspace_id=workspace_id)
    all_keys = set(current_groups) | set(previous_groups)
    if not all_keys and not plan.group_by:
        all_keys.add(tuple())

    sort_metric = plan.sort_metric or plan.metrics[0]

    def sort_value(key: GroupKey) -> float:
        value = _metric_value(sort_metric, current_groups.get(key, _GroupValues()))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(
        all_keys,
        key=lambda key: (sort_value(key), _group_token(key)),
        reverse=plan.sort_direction == "desc",
    )[: plan.limit]

    rows: list[AnalyticsBIResultRow] = []
    for key in ordered:
        current_values = current_groups.get(key, _GroupValues())
        previous_values = previous_groups.get(key, _GroupValues())
        metrics: list[AnalyticsBIMetricRead] = []
        for metric in plan.metrics:
            current_value = _metric_value(metric, current_values)
            if previous_period is None:
                metrics.append(_metric_read(metric, current_value, currency=plan.currency))
            else:
                previous_value = _metric_value(metric, previous_values)
                metrics.extend(
                    _comparison_metrics(
                        metric=metric,
                        current=current_value,
                        previous=previous_value,
                        currency=plan.currency,
                    )
                )
        rows.append(
            AnalyticsBIResultRow(
                key=_group_token(key),
                label=_group_label(key, labels),
                secondary_label=None,
                metrics=metrics,
            )
        )

    period_label = active_period.label
    if previous_period is not None:
        period_label = f"{active_period.label} مقارنة بالفترة السابقة المساوية"
    group_text = " / ".join(plan.group_by) if plan.group_by else "إجمالي العيادة"
    answer = f"نفذت التحليل المركب على {group_text} وطلعت {len(rows)} نتيجة."
    return AnalyticsBusinessAnswerRead(
        question=question,
        plan=plan,
        period_label=period_label,
        answer=answer,
        definitions=_definitions(plan),
        rows=rows,
        model=model,
    )
