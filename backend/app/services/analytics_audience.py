from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session

from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.patient import Patient
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.schemas.analytics_bi import AnalyticsBIMetricRead, AnalyticsBIResultRow
from app.schemas.analytics_composable import AnalyticsAudiencePlan
from app.services.analytics_bi import AnalyticsBIError


@dataclass(frozen=True)
class AnalyticsAudienceExecution:
    period_label: str
    answer: str
    definitions: list[str]
    rows: list[AnalyticsBIResultRow]


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _as_uuids(values: list[str]) -> list[UUID]:
    result: list[UUID] = []
    for value in values:
        try:
            result.append(UUID(value))
        except ValueError as exc:
            raise AnalyticsBIError("Audience plan contains an invalid canonical UUID.") from exc
    return result


def validate_audience_plan_entities(
    plan: AnalyticsAudiencePlan,
    *,
    catalog: dict[str, list[dict[str, str]]],
) -> AnalyticsAudiencePlan:
    for field_name, collection_name in (
        ("service_ids", "services"),
        ("branch_ids", "branches"),
        ("doctor_ids", "doctors"),
    ):
        allowed = {str(item.get("id")) for item in catalog.get(collection_name, [])}
        requested = list(getattr(plan, field_name))
        if any(value not in allowed for value in requested):
            raise AnalyticsBIError(
                f"Audience plan referenced an unknown canonical {collection_name[:-1]} id."
            )
    return plan




_STATUS_LABELS = {
    "pending": "pending",
    "confirmed": "confirmed",
    "checked_in": "checked_in",
    "in_progress": "in_progress",
    "completed": "completed",
    "cancelled": "cancelled",
    "no_show": "no_show",
    "rescheduled": "rescheduled",
}


def _scope_appointment_clauses_without_status(
    *,
    workspace_id: UUID,
    plan: AnalyticsAudiencePlan,
    current: datetime,
) -> list[Any]:
    clauses: list[Any] = [Appointment.workspace_id == workspace_id]
    if plan.lookback_days is not None:
        clauses.append(Appointment.start_at >= current - timedelta(days=plan.lookback_days))
    if plan.service_ids:
        clauses.append(Appointment.service_id.in_(_as_uuids(plan.service_ids)))
    if plan.branch_ids:
        clauses.append(Appointment.branch_id.in_(_as_uuids(plan.branch_ids)))
    if plan.doctor_ids:
        clauses.append(Appointment.doctor_id.in_(_as_uuids(plan.doctor_ids)))
    return clauses


def _selected_service_names(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsAudiencePlan,
) -> list[str]:
    if not plan.service_ids:
        return []
    ids = _as_uuids(plan.service_ids)
    rows = db.execute(
        select(Service.name).where(
            Service.workspace_id == workspace_id,
            Service.id.in_(ids),
        ).order_by(Service.name, Service.id)
    ).all()
    return [str(row.name) for row in rows]


def _zero_result_diagnostics(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsAudiencePlan,
    current: datetime,
) -> tuple[str | None, list[str]]:
    """Explain a zero audience without silently broadening the query.

    We intentionally do not reinterpret past confirmed/pending appointments as
    completed sessions. The diagnostic inspects the same canonical scope while
    temporarily removing only the appointment-status predicate, so staff can see
    whether the zero came from status semantics versus service/date grounding.
    """
    status_rows = db.execute(
        select(Appointment.status, func.count(Appointment.id).label("count"))
        .where(*_scope_appointment_clauses_without_status(
            workspace_id=workspace_id, plan=plan, current=current
        ))
        .group_by(Appointment.status)
        .order_by(Appointment.status)
    ).all()
    service_names = _selected_service_names(
        db, workspace_id=workspace_id, plan=plan
    )
    notes: list[str] = []
    if service_names:
        notes.append("الخدمات التي طبقتها الخطة: " + "، ".join(service_names) + ".")
    requested = ", ".join(plan.appointment_statuses)
    notes.append("حالات المواعيد المطلوبة في الخطة: " + requested + ".")

    if not status_rows:
        return (
            "لم أجد مواعيد تطابق الخدمة/الفرع/الدكتور والفترة المحددة قبل تطبيق فلتر حالة الموعد.",
            notes,
        )

    status_counts = {str(row.status): int(row.count or 0) for row in status_rows}
    total = sum(status_counts.values())
    rendered = "، ".join(
        f"{_STATUS_LABELS.get(status, status)}={count}"
        for status, count in status_counts.items()
    )
    requested_total = sum(status_counts.get(status, 0) for status in plan.appointment_statuses)
    notes.append(f"لنفس النطاق قبل فلتر الحالة: {total} موعد ({rendered}).")
    if requested_total == 0:
        return (
            "فيه مواعيد لنفس الخدمة والفترة، لكن ولا واحد حالته من الحالات التي تعتبرها الخطة جلسة مطابقة. "
            "Linka لم تحوّل confirmed/pending القديمة إلى completed تلقائيًا حتى لا تحسب حجزًا لم يتم كجلسة فعلية.",
            notes,
        )
    return (
        "فيه مواعيد تطابق الخدمة والفترة والحالة، لكن فلاتر المريض الإضافية استبعدتها "
        "(مثل حالة المريض أو الحجز القادم أو consent أو شرط القيمة).",
        notes,
    )


def _metric(
    key: str,
    label: str,
    value: int | float | str,
    *,
    currency: str | None = None,
) -> AnalyticsBIMetricRead:
    return AnalyticsBIMetricRead(key=key, label=label, value=value, currency=currency)


def _full_name(first_name: str | None, last_name: str | None) -> str:
    return " ".join(part for part in (first_name, last_name) if part).strip() or "عميل"


def _matching_appointment_clauses(
    *,
    workspace_id: UUID,
    plan: AnalyticsAudiencePlan,
    current: datetime,
) -> list[Any]:
    clauses: list[Any] = [
        Appointment.workspace_id == workspace_id,
        Appointment.status.in_(plan.appointment_statuses),
    ]
    if plan.lookback_days is not None:
        clauses.append(Appointment.start_at >= current - timedelta(days=plan.lookback_days))
    if plan.service_ids:
        clauses.append(Appointment.service_id.in_(_as_uuids(plan.service_ids)))
    if plan.branch_ids:
        clauses.append(Appointment.branch_id.in_(_as_uuids(plan.branch_ids)))
    if plan.doctor_ids:
        clauses.append(Appointment.doctor_id.in_(_as_uuids(plan.doctor_ids)))
    return clauses


def _patient_value_source(
    *,
    workspace_id: UUID,
    plan: AnalyticsAudiencePlan,
    current: datetime,
):
    if not plan.currency:
        return None
    start_at = current - timedelta(days=plan.lookback_days) if plan.lookback_days is not None else None
    payment_clauses: list[Any] = [
        PaymentTransaction.workspace_id == workspace_id,
        PaymentTransaction.currency == plan.currency,
    ]
    if start_at is not None:
        payment_clauses.append(PaymentTransaction.created_at >= start_at)

    entity_filtered = bool(plan.service_ids or plan.branch_ids or plan.doctor_ids)
    signed_amount = case(
        (PaymentTransaction.transaction_type == "payment", PaymentTransaction.amount_minor),
        else_=-PaymentTransaction.amount_minor,
    )
    if not entity_filtered:
        return (
            select(
                PaymentTransaction.patient_id.label("patient_id"),
                func.coalesce(func.sum(signed_amount), 0).label("net_minor"),
            )
            .where(*payment_clauses)
            .group_by(PaymentTransaction.patient_id)
            .subquery()
        )

    allocation_signed = case(
        (PaymentTransaction.transaction_type == "payment", PaymentAllocation.amount_minor),
        else_=-PaymentAllocation.amount_minor,
    )
    appointment_clauses: list[Any] = [Appointment.workspace_id == workspace_id]
    if plan.service_ids:
        appointment_clauses.append(Appointment.service_id.in_(_as_uuids(plan.service_ids)))
    if plan.branch_ids:
        appointment_clauses.append(Appointment.branch_id.in_(_as_uuids(plan.branch_ids)))
    if plan.doctor_ids:
        appointment_clauses.append(Appointment.doctor_id.in_(_as_uuids(plan.doctor_ids)))
    return (
        select(
            PaymentTransaction.patient_id.label("patient_id"),
            func.coalesce(func.sum(allocation_signed), 0).label("net_minor"),
        )
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
        .where(*payment_clauses, *appointment_clauses)
        .group_by(PaymentTransaction.patient_id)
        .subquery()
    )


def audience_period_label(plan: AnalyticsAudiencePlan) -> str:
    parts: list[str] = []
    if plan.lookback_days is not None:
        parts.append(f"نشاط آخر {plan.lookback_days} يوم")
    if plan.inactivity_days is not None:
        parts.append(f"آخر نشاط أقدم من {plan.inactivity_days} يوم")
    if not parts:
        return "كل التاريخ المتاح"
    return " · ".join(parts)


def execute_audience_plan(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsAudiencePlan,
    now: datetime | None = None,
) -> AnalyticsAudienceExecution:
    current = _now(now)
    matching = (
        select(
            Appointment.patient_id.label("patient_id"),
            func.count(Appointment.id).label("matching_visits"),
            func.max(Appointment.start_at).label("last_activity_at"),
        )
        .where(*_matching_appointment_clauses(workspace_id=workspace_id, plan=plan, current=current))
        .group_by(Appointment.patient_id)
        .subquery()
    )

    future_active = exists(
        select(Appointment.id).where(
            Appointment.workspace_id == workspace_id,
            Appointment.patient_id == Patient.id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.start_at >= current,
        )
    )

    where: list[Any] = [
        Patient.workspace_id == workspace_id,
        Patient.status.in_(plan.patient_statuses),
        matching.c.matching_visits >= plan.min_matching_visits,
    ]
    if plan.max_matching_visits is not None:
        where.append(matching.c.matching_visits <= plan.max_matching_visits)
    if plan.inactivity_days is not None:
        where.append(matching.c.last_activity_at <= current - timedelta(days=plan.inactivity_days))
    if plan.has_future_appointment is True:
        where.append(future_active)
    elif plan.has_future_appointment is False:
        where.append(~future_active)
    if plan.marketing_consent is not None:
        where.append(Patient.marketing_consent.is_(plan.marketing_consent))

    value_source = _patient_value_source(workspace_id=workspace_id, plan=plan, current=current)
    select_columns: list[Any] = [
        Patient.id,
        Patient.first_name,
        Patient.last_name,
        Patient.phone,
        Patient.source_created_at,
        Patient.created_at,
        matching.c.matching_visits,
        matching.c.last_activity_at,
    ]
    stmt = select(*select_columns).join(matching, matching.c.patient_id == Patient.id)
    if value_source is not None:
        stmt = stmt.outerjoin(value_source, value_source.c.patient_id == Patient.id).add_columns(
            func.coalesce(value_source.c.net_minor, 0).label("net_minor")
        )
        if plan.min_net_paid_minor is not None:
            where.append(func.coalesce(value_source.c.net_minor, 0) >= plan.min_net_paid_minor)
        if plan.max_net_paid_minor is not None:
            where.append(func.coalesce(value_source.c.net_minor, 0) <= plan.max_net_paid_minor)
    stmt = stmt.where(*where)

    effective_first_seen = func.coalesce(Patient.source_created_at, Patient.created_at)
    if plan.sort_by == "last_activity_asc":
        stmt = stmt.order_by(matching.c.last_activity_at.asc(), Patient.id)
    elif plan.sort_by == "matching_visits_desc":
        stmt = stmt.order_by(matching.c.matching_visits.desc(), matching.c.last_activity_at.desc(), Patient.id)
    elif plan.sort_by == "net_paid_desc":
        if value_source is None:
            raise AnalyticsBIError("net_paid_desc requires a currency-backed patient value query.")
        stmt = stmt.order_by(func.coalesce(value_source.c.net_minor, 0).desc(), Patient.id)
    elif plan.sort_by == "first_seen_desc":
        stmt = stmt.order_by(effective_first_seen.desc(), Patient.id)
    else:
        stmt = stmt.order_by(matching.c.last_activity_at.desc(), matching.c.matching_visits.desc(), Patient.id)

    raw_rows = db.execute(stmt.limit(plan.limit)).all()
    rows: list[AnalyticsBIResultRow] = []
    for row in raw_rows:
        metrics: list[AnalyticsBIMetricRead] = [
            _metric("matching_visits", "زيارات مطابقة", int(row.matching_visits or 0)),
            _metric(
                "last_activity_at",
                "آخر نشاط مطابق",
                row.last_activity_at.isoformat() if row.last_activity_at else "—",
            ),
        ]
        if value_source is not None:
            metrics.append(
                _metric(
                    "net_paid_minor",
                    "صافي المدفوع",
                    int(row.net_minor or 0),
                    currency=plan.currency,
                )
            )
        rows.append(
            AnalyticsBIResultRow(
                key=str(row.id),
                label=_full_name(row.first_name, row.last_name),
                secondary_label=row.phone,
                metrics=metrics,
            )
        )

    definitions = [
        "المجموعة مبنية على بيانات المرضى والمواعيد canonical داخل Linka، وليس على تخمين نصي من الـAI.",
        "عدد الزيارات وآخر نشاط يُحسبان فقط من المواعيد التي تطابق الخدمة/الفرع/الدكتور والحالات والفترة المحددة في الخطة.",
    ]
    service_names = _selected_service_names(db, workspace_id=workspace_id, plan=plan)
    if service_names:
        definitions.append("الخدمات التي طبقتها الخطة: " + "، ".join(service_names) + ".")
    if plan.has_future_appointment is False:
        definitions.append("تم استبعاد أي مريض لديه موعد نشط قادم في Linka.")
    if value_source is not None:
        definitions.append(
            "القيمة المالية = payments ناقص refunds؛ وعند فلترة خدمة/فرع/دكتور تُستخدم payment allocations الصريحة فقط."
        )
    answer = f"لقيت {len(rows)} عميل مطابق للشروط المطلوبة."
    if not rows:
        diagnostic, diagnostic_notes = _zero_result_diagnostics(
            db, workspace_id=workspace_id, plan=plan, current=current
        )
        if diagnostic:
            answer = "لقيت 0 عميل مطابق للشروط المطلوبة. " + diagnostic
        for note in diagnostic_notes:
            if note not in definitions:
                definitions.append(note)
    return AnalyticsAudienceExecution(
        period_label=audience_period_label(plan),
        answer=answer,
        definitions=definitions,
        rows=rows,
    )
