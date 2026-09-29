from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, exists, extract, func, select
from sqlalchemy.orm import Session

from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.schemas.analytics_bi import (
    AnalyticsBIAnswerRead,
    AnalyticsBIMetricRead,
    AnalyticsBIPlan,
    AnalyticsBIResultRow,
)
from app.schemas.crm import normalize_phone
from app.services.patient_history import build_patient_history_context


class AnalyticsBIError(ValueError):
    pass


def analytics_entity_catalog(db: Session, *, workspace_id: UUID) -> dict[str, list[dict[str, str]]]:
    """Small canonical entity catalog for the semantic planner.

    Analytics is always executed against Linka's canonical tables, even when the
    workspace sync source is an external connector. The planner receives names
    and canonical IDs only; it never receives patient rows or financial facts.
    """
    services = db.execute(
        select(Service.id, Service.name)
        .where(Service.workspace_id == workspace_id, Service.is_active.is_(True))
        .order_by(Service.name, Service.id)
    ).all()
    branches = db.execute(
        select(Branch.id, Branch.name)
        .where(Branch.workspace_id == workspace_id, Branch.is_active.is_(True))
        .order_by(Branch.name, Branch.id)
    ).all()
    doctors = db.execute(
        select(Doctor.id, Staff.first_name, Staff.last_name)
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(
            Doctor.workspace_id == workspace_id,
            Doctor.is_active.is_(True),
            Staff.is_active.is_(True),
        )
        .order_by(Staff.first_name, Staff.last_name, Doctor.id)
    ).all()
    return {
        "services": [{"id": str(row.id), "name": row.name} for row in services],
        "branches": [{"id": str(row.id), "name": row.name} for row in branches],
        "doctors": [
            {
                "id": str(row.id),
                "name": " ".join(part for part in (row.first_name, row.last_name) if part).strip()
                or "Doctor",
            }
            for row in doctors
        ],
    }


def validate_analytics_plan_entities(
    plan: AnalyticsBIPlan,
    *,
    catalog: dict[str, list[dict[str, str]]],
) -> AnalyticsBIPlan:
    for field_name, collection_name in (
        ("service_ids", "services"),
        ("branch_ids", "branches"),
        ("doctor_ids", "doctors"),
    ):
        allowed = {str(item.get("id")) for item in catalog.get(collection_name, [])}
        requested = list(getattr(plan, field_name))
        invalid = [value for value in requested if value not in allowed]
        if invalid:
            raise AnalyticsBIError(
                f"Analytics plan referenced an unknown canonical {collection_name[:-1]} id."
            )

    has_entity_filters = bool(plan.service_ids or plan.branch_ids or plan.doctor_ids)
    if plan.operation in {"clinic_summary", "new_patients_trend", "patient_history_lookup"} and has_entity_filters:
        raise AnalyticsBIError(
            f"{plan.operation} does not accept service/branch/doctor filters; choose a scoped operation instead."
        )
    if plan.inactivity_days is not None and plan.operation != "lapsed_patients":
        raise AnalyticsBIError("inactivity_days is only valid for lapsed_patients.")
    if plan.currency is not None and plan.operation not in {
        "clinic_summary",
        "revenue_trend",
        "top_value_patients",
    }:
        raise AnalyticsBIError(f"currency is not a supported filter for {plan.operation}.")
    has_patient_hint = bool((plan.patient_name or "").strip() or (plan.patient_phone or "").strip())
    if plan.operation == "patient_history_lookup":
        if not has_patient_hint:
            raise AnalyticsBIError("patient_history_lookup requires an exact patient name or phone from the question.")
        if plan.lookback_days is not None or plan.inactivity_days is not None or plan.currency is not None:
            raise AnalyticsBIError("patient_history_lookup does not accept analytics period/currency filters.")
    elif has_patient_hint:
        raise AnalyticsBIError(f"patient lookup hints are not valid for {plan.operation}.")
    return plan


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _period_start(plan: AnalyticsBIPlan, current: datetime) -> datetime | None:
    if plan.lookback_days is None:
        return None
    return current - timedelta(days=plan.lookback_days)


def _period_label(plan: AnalyticsBIPlan) -> str:
    if plan.lookback_days is None:
        return "كل التاريخ المتاح"
    return f"آخر {plan.lookback_days} يوم"


def _as_uuids(values: list[str]) -> list[UUID]:
    result: list[UUID] = []
    for value in values:
        try:
            result.append(UUID(value))
        except ValueError as exc:
            raise AnalyticsBIError("Analytics plan contains an invalid canonical UUID.") from exc
    return result


def _appointment_where(
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
    include_rescheduled: bool = False,
) -> list[Any]:
    clauses: list[Any] = [Appointment.workspace_id == workspace_id]
    if not include_rescheduled:
        clauses.append(Appointment.status != "rescheduled")
    if start_at is not None:
        clauses.append(Appointment.start_at >= start_at)
    if plan.service_ids:
        clauses.append(Appointment.service_id.in_(_as_uuids(plan.service_ids)))
    if plan.branch_ids:
        clauses.append(Appointment.branch_id.in_(_as_uuids(plan.branch_ids)))
    if plan.doctor_ids:
        clauses.append(Appointment.doctor_id.in_(_as_uuids(plan.doctor_ids)))
    return clauses


def _payment_where(
    *, workspace_id: UUID, plan: AnalyticsBIPlan, start_at: datetime | None
) -> list[Any]:
    clauses: list[Any] = [PaymentTransaction.workspace_id == workspace_id]
    if start_at is not None:
        clauses.append(PaymentTransaction.created_at >= start_at)
    if plan.currency:
        clauses.append(PaymentTransaction.currency == plan.currency)
    return clauses


def _metric(
    key: str,
    label: str,
    value: int | float | str,
    *,
    currency: str | None = None,
) -> AnalyticsBIMetricRead:
    return AnalyticsBIMetricRead(key=key, label=label, value=value, currency=currency)


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 1)


def _full_name(first_name: str | None, last_name: str | None) -> str:
    return " ".join(part for part in (first_name, last_name) if part).strip() or "Unknown"


def _patient_secondary(phone: str | None, last_at: datetime | None = None) -> str | None:
    parts: list[str] = []
    if phone:
        parts.append(phone)
    if last_at:
        parts.append(f"آخر زيارة {last_at.date().isoformat()}")
    return " · ".join(parts) or None


def _summary(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    clauses = _appointment_where(workspace_id=workspace_id, plan=plan, start_at=start_at)
    counts = db.execute(
        select(
            func.count(Appointment.id).label("total"),
            func.count(Appointment.id).filter(Appointment.status == "completed").label("completed"),
            func.count(Appointment.id).filter(Appointment.status == "cancelled").label("cancelled"),
            func.count(Appointment.id).filter(Appointment.status == "no_show").label("no_show"),
        ).where(*clauses)
    ).one()
    patient_clauses: list[Any] = [Patient.workspace_id == workspace_id]
    if start_at is not None:
        patient_clauses.append(func.coalesce(Patient.source_created_at, Patient.created_at) >= start_at)
    new_patients = int(db.scalar(select(func.count(Patient.id)).where(*patient_clauses)) or 0)

    payment_rows = db.execute(
        select(
            PaymentTransaction.currency,
            func.coalesce(
                func.sum(
                    case(
                        (PaymentTransaction.transaction_type == "payment", PaymentTransaction.amount_minor),
                        else_=0,
                    )
                ),
                0,
            ).label("gross"),
            func.coalesce(
                func.sum(
                    case(
                        (PaymentTransaction.transaction_type == "refund", PaymentTransaction.amount_minor),
                        else_=0,
                    )
                ),
                0,
            ).label("refunds"),
        )
        .where(*_payment_where(workspace_id=workspace_id, plan=plan, start_at=start_at))
        .group_by(PaymentTransaction.currency)
        .order_by(PaymentTransaction.currency)
    ).all()

    total = int(counts.total or 0)
    completed = int(counts.completed or 0)
    cancelled = int(counts.cancelled or 0)
    no_show = int(counts.no_show or 0)
    rows = [
        AnalyticsBIResultRow(
            label="تشغيل العيادة",
            metrics=[
                _metric("appointments", "الحجوزات", total),
                _metric("completed", "مكتمل", completed),
                _metric("cancelled", "ملغي", cancelled),
                _metric("no_show", "No-show", no_show),
                _metric("completion_rate", "نسبة الاكتمال", _pct(completed, total)),
                _metric("new_patients", "عملاء جدد", new_patients),
            ],
        )
    ]
    for row in payment_rows:
        gross = int(row.gross or 0)
        refunds = int(row.refunds or 0)
        rows.append(
            AnalyticsBIResultRow(
                label=f"المدفوعات {row.currency}",
                metrics=[
                    _metric("gross_paid_minor", "إجمالي الدفعات", gross, currency=row.currency),
                    _metric("refunded_minor", "Refunds", refunds, currency=row.currency),
                    _metric("net_paid_minor", "صافي المدفوع", gross - refunds, currency=row.currency),
                ],
            )
        )
    answer = f"الفترة فيها {total} حجز، منهم {completed} مكتمل، و{new_patients} عميل جديد."
    definitions = [
        "العميل الجديد يُحسب من source_created_at عند توفره، وإلا created_at في Linka.",
        "صافي المدفوع = payment transactions ناقص refund transactions داخل الفترة.",
    ]
    return answer, definitions, rows


def _appointment_outcomes(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    counts = db.execute(
        select(
            func.count(Appointment.id).label("total"),
            func.count(Appointment.id).filter(Appointment.status == "completed").label("completed"),
            func.count(Appointment.id).filter(Appointment.status == "no_show").label("no_show"),
            func.count(Appointment.id).filter(Appointment.status == "cancelled").label("cancelled"),
        ).where(*_appointment_where(workspace_id=workspace_id, plan=plan, start_at=start_at))
    ).one()
    total = int(counts.total or 0)
    completed = int(counts.completed or 0)
    no_show = int(counts.no_show or 0)
    cancelled = int(counts.cancelled or 0)
    attendance_denominator = completed + no_show
    rows = [
        AnalyticsBIResultRow(
            label="نتائج المواعيد",
            metrics=[
                _metric("appointments", "الحجوزات", total),
                _metric("completed", "مكتمل", completed),
                _metric("attendance_rate", "نسبة الحضور", _pct(completed, attendance_denominator)),
                _metric("no_show_rate", "No-show", _pct(no_show, attendance_denominator)),
                _metric("cancellation_rate", "الإلغاء", _pct(cancelled, total)),
            ],
        )
    ]
    return (
        f"نسبة الحضور {_pct(completed, attendance_denominator)}% ونسبة الإلغاء {_pct(cancelled, total)}%.",
        [
            "نسبة الحضور = completed ÷ (completed + no-show).",
            "نسبة الإلغاء = cancelled ÷ كل المواعيد غير rescheduled.",
        ],
        rows,
    )


def _entity_performance(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
    entity_kind: str,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    clauses = _appointment_where(workspace_id=workspace_id, plan=plan, start_at=start_at)
    if entity_kind == "service":
        stmt = (
            select(
                Service.id.label("entity_id"),
                Service.name.label("name"),
                func.count(Appointment.id).label("appointments"),
                func.count(Appointment.id).filter(Appointment.status == "completed").label("completed"),
                func.count(Appointment.id).filter(Appointment.status == "no_show").label("no_show"),
                func.count(Appointment.id).filter(Appointment.status == "cancelled").label("cancelled"),
                func.count(func.distinct(Appointment.patient_id))
                .filter(Appointment.status == "completed")
                .label("patients"),
            )
            .join(
                Appointment,
                (Appointment.workspace_id == Service.workspace_id) & (Appointment.service_id == Service.id),
            )
            .where(*clauses)
            .group_by(Service.id, Service.name)
        )
    elif entity_kind == "branch":
        stmt = (
            select(
                Branch.id.label("entity_id"),
                Branch.name.label("name"),
                func.count(Appointment.id).label("appointments"),
                func.count(Appointment.id).filter(Appointment.status == "completed").label("completed"),
                func.count(Appointment.id).filter(Appointment.status == "no_show").label("no_show"),
                func.count(Appointment.id).filter(Appointment.status == "cancelled").label("cancelled"),
                func.count(func.distinct(Appointment.patient_id))
                .filter(Appointment.status == "completed")
                .label("patients"),
            )
            .join(
                Appointment,
                (Appointment.workspace_id == Branch.workspace_id) & (Appointment.branch_id == Branch.id),
            )
            .where(*clauses)
            .group_by(Branch.id, Branch.name)
        )
    elif entity_kind == "doctor":
        stmt = (
            select(
                Doctor.id.label("entity_id"),
                Staff.first_name.label("first_name"),
                Staff.last_name.label("last_name"),
                func.count(Appointment.id).label("appointments"),
                func.count(Appointment.id).filter(Appointment.status == "completed").label("completed"),
                func.count(Appointment.id).filter(Appointment.status == "no_show").label("no_show"),
                func.count(Appointment.id).filter(Appointment.status == "cancelled").label("cancelled"),
                func.count(func.distinct(Appointment.patient_id))
                .filter(Appointment.status == "completed")
                .label("patients"),
            )
            .join(Staff, (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id))
            .join(
                Appointment,
                (Appointment.workspace_id == Doctor.workspace_id) & (Appointment.doctor_id == Doctor.id),
            )
            .where(*clauses)
            .group_by(Doctor.id, Staff.first_name, Staff.last_name)
        )
    else:
        raise AnalyticsBIError("Unsupported analytics entity kind.")

    raw_rows = db.execute(stmt).all()
    normalized: list[tuple[Any, str, int, int, int, int, int]] = []
    for row in raw_rows:
        name = (
            _full_name(row.first_name, row.last_name)
            if entity_kind == "doctor"
            else row.name
        )
        normalized.append(
            (
                row.entity_id,
                name,
                int(row.appointments or 0),
                int(row.completed or 0),
                int(row.no_show or 0),
                int(row.cancelled or 0),
                int(row.patients or 0),
            )
        )
    normalized.sort(key=lambda item: (-item[3], -item[2], item[1]))
    normalized = normalized[: plan.limit]
    rows = [
        AnalyticsBIResultRow(
            key=str(entity_id),
            label=name,
            metrics=[
                _metric("appointments", "الحجوزات", total),
                _metric("completed", "مكتمل", completed),
                _metric("unique_patients", "عملاء مكتملون", patients),
                _metric("completion_rate", "نسبة الاكتمال", _pct(completed, total)),
                _metric("no_show", "No-show", no_show),
                _metric("cancelled", "ملغي", cancelled),
            ],
        )
        for entity_id, name, total, completed, no_show, cancelled, patients in normalized
    ]
    label = {"service": "الخدمات", "branch": "الفروع", "doctor": "الأطباء"}[entity_kind]
    answer = f"تم ترتيب {label} حسب عدد المواعيد المكتملة في الفترة."
    return answer, ["نسبة الاكتمال = completed ÷ كل المواعيد غير rescheduled لنفس الكيان."], rows


def _service_retention(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    clauses = _appointment_where(workspace_id=workspace_id, plan=plan, start_at=start_at)
    clauses.append(Appointment.status == "completed")
    patient_service = (
        select(
            Appointment.service_id.label("service_id"),
            Appointment.patient_id.label("patient_id"),
            func.count(Appointment.id).label("visits"),
        )
        .where(*clauses)
        .group_by(Appointment.service_id, Appointment.patient_id)
        .subquery()
    )
    rows_db = db.execute(
        select(
            Service.id,
            Service.name,
            func.count(patient_service.c.patient_id).label("patients"),
            func.coalesce(
                func.sum(case((patient_service.c.visits >= 2, 1), else_=0)),
                0,
            ).label("repeat_patients"),
            func.coalesce(func.sum(patient_service.c.visits), 0).label("visits"),
        )
        .join(
            patient_service,
            patient_service.c.service_id == Service.id,
        )
        .where(Service.workspace_id == workspace_id)
        .group_by(Service.id, Service.name)
    ).all()
    normalized = []
    for row in rows_db:
        patients = int(row.patients or 0)
        repeat = int(row.repeat_patients or 0)
        normalized.append(
            (row.id, row.name, patients, repeat, int(row.visits or 0), _pct(repeat, patients))
        )
    normalized.sort(key=lambda item: (-item[5], -item[3], -item[4], item[1]))
    normalized = normalized[: plan.limit]
    rows = [
        AnalyticsBIResultRow(
            key=str(service_id),
            label=name,
            metrics=[
                _metric("service_repeat_rate", "Same-service repeat rate", rate),
                _metric("repeat_patients", "عملاء كرروا الخدمة", repeat),
                _metric("unique_patients", "عملاء الخدمة", patients),
                _metric("completed_visits", "جلسات مكتملة", visits),
            ],
        )
        for service_id, name, patients, repeat, visits, rate in normalized
    ]
    if rows:
        answer = f"أعلى خدمة في نفس-service repeat rate هي {rows[0].label}."
    else:
        answer = "مفيش زيارات مكتملة كفاية لحساب service retention في الفترة."
    return (
        answer,
        [
            "Same-service repeat rate = العملاء اللي عندهم 2+ جلسة مكتملة لنفس الخدمة ÷ كل عملاء الخدمة المكتملين.",
            "المقياس لا يفترض إن زيارة خدمة مختلفة تعتبر retention لنفس الخدمة.",
        ],
        rows,
    )


def _top_repeat_patients(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    clauses = _appointment_where(workspace_id=workspace_id, plan=plan, start_at=start_at)
    clauses.append(Appointment.status == "completed")
    rows_db = db.execute(
        select(
            Patient.id,
            Patient.first_name,
            Patient.last_name,
            Patient.phone,
            func.count(Appointment.id).label("completed"),
            func.max(Appointment.start_at).label("last_at"),
        )
        .join(
            Appointment,
            (Appointment.workspace_id == Patient.workspace_id) & (Appointment.patient_id == Patient.id),
        )
        .where(Patient.workspace_id == workspace_id, *clauses)
        .group_by(Patient.id, Patient.first_name, Patient.last_name, Patient.phone)
        .having(func.count(Appointment.id) >= 2)
        .order_by(func.count(Appointment.id).desc(), func.max(Appointment.start_at).desc(), Patient.id)
        .limit(plan.limit)
    ).all()
    rows = [
        AnalyticsBIResultRow(
            key=str(row.id),
            label=_full_name(row.first_name, row.last_name),
            secondary_label=_patient_secondary(row.phone, row.last_at),
            metrics=[_metric("completed_visits", "زيارات مكتملة", int(row.completed or 0))],
        )
        for row in rows_db
    ]
    return (
        f"لقيت {len(rows)} عميل ضمن أعلى العملاء تكرارًا في الفترة.",
        ["التكرار هنا = عدد المواعيد المكتملة للعميل داخل نطاق السؤال."],
        rows,
    )


def _lapsed_patients(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    current: datetime,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    inactivity_days = plan.inactivity_days or 180
    cutoff = current - timedelta(days=inactivity_days)
    completed_clauses = _appointment_where(
        workspace_id=workspace_id,
        plan=plan,
        start_at=None,
    )
    completed_clauses.append(Appointment.status == "completed")
    activity = (
        select(
            Appointment.patient_id.label("patient_id"),
            func.count(Appointment.id).label("completed"),
            func.max(Appointment.start_at).label("last_at"),
        )
        .where(*completed_clauses)
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
        Patient.status != "blocked",
        activity.c.last_at <= cutoff,
        ~future_active,
    ]
    if start_at is not None:
        where.append(activity.c.last_at >= start_at)
    rows_db = db.execute(
        select(
            Patient.id,
            Patient.first_name,
            Patient.last_name,
            Patient.phone,
            activity.c.completed,
            activity.c.last_at,
        )
        .join(activity, activity.c.patient_id == Patient.id)
        .where(*where)
        .order_by(activity.c.last_at.desc(), activity.c.completed.desc(), Patient.id)
        .limit(plan.limit)
    ).all()
    rows = [
        AnalyticsBIResultRow(
            key=str(row.id),
            label=_full_name(row.first_name, row.last_name),
            secondary_label=_patient_secondary(row.phone, row.last_at),
            metrics=[
                _metric("completed_visits", "زيارات مكتملة", int(row.completed or 0)),
                _metric("inactive_days", "بدون زيارة منذ", max((current - row.last_at).days, 0)),
            ],
        )
        for row in rows_db
    ]
    return (
        f"لقيت {len(rows)} عميل آخر زيارة مكتملة لهم من {inactivity_days}+ يوم ومفيش لهم موعد نشط قادم.",
        [
            "Lapsed = آخر موعد مكتمل أقدم من حد عدم النشاط، مع عدم وجود موعد نشط قادم.",
            "العملاء blocked مستبعدين من قائمة lapsed التشغيلية.",
        ],
        rows,
    )


def _patient_value_source(
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
):
    entity_filtered = bool(plan.service_ids or plan.branch_ids or plan.doctor_ids)
    if not entity_filtered:
        return (
            select(
                PaymentTransaction.patient_id.label("patient_id"),
                PaymentTransaction.currency.label("currency"),
                func.coalesce(
                    func.sum(
                        case(
                            (PaymentTransaction.transaction_type == "payment", PaymentTransaction.amount_minor),
                            else_=-PaymentTransaction.amount_minor,
                        )
                    ),
                    0,
                ).label("net_minor"),
            )
            .where(*_payment_where(workspace_id=workspace_id, plan=plan, start_at=start_at))
            .group_by(PaymentTransaction.patient_id, PaymentTransaction.currency)
            .subquery()
        )

    appointment_clauses = _appointment_where(
        workspace_id=workspace_id,
        plan=plan,
        start_at=None,
        include_rescheduled=True,
    )
    payment_clauses = _payment_where(workspace_id=workspace_id, plan=plan, start_at=start_at)
    return (
        select(
            PaymentTransaction.patient_id.label("patient_id"),
            PaymentTransaction.currency.label("currency"),
            func.coalesce(
                func.sum(
                    case(
                        (PaymentTransaction.transaction_type == "payment", PaymentAllocation.amount_minor),
                        else_=-PaymentAllocation.amount_minor,
                    )
                ),
                0,
            ).label("net_minor"),
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
        .group_by(PaymentTransaction.patient_id, PaymentTransaction.currency)
        .subquery()
    )


def _top_value_patients(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    value_source = _patient_value_source(
        workspace_id=workspace_id,
        plan=plan,
        start_at=start_at,
    )
    rows_db = db.execute(
        select(
            Patient.id,
            Patient.first_name,
            Patient.last_name,
            Patient.phone,
            value_source.c.currency,
            value_source.c.net_minor,
        )
        .join(value_source, value_source.c.patient_id == Patient.id)
        .where(Patient.workspace_id == workspace_id)
        .order_by(value_source.c.net_minor.desc(), Patient.id, value_source.c.currency)
        .limit(plan.limit)
    ).all()
    rows = [
        AnalyticsBIResultRow(
            key=str(row.id),
            label=_full_name(row.first_name, row.last_name),
            secondary_label=row.phone,
            metrics=[
                _metric(
                    "net_paid_minor",
                    "صافي المدفوع",
                    int(row.net_minor or 0),
                    currency=row.currency,
                )
            ],
        )
        for row in rows_db
    ]
    return (
        f"تم ترتيب أعلى {len(rows)} صف حسب صافي المدفوع المسجل.",
        [
            "صافي قيمة العميل = payments ناقص refunds في نطاق السؤال.",
            "عند فلترة خدمة/فرع/دكتور، تُستخدم payment allocations الصريحة فقط؛ لا يتم تخمين توزيع المدفوعات.",
        ],
        rows,
    )


def _trend_bucket(plan: AnalyticsBIPlan) -> str:
    if plan.lookback_days is not None and plan.lookback_days <= 90:
        return "day"
    return "month"


def _bucket_expressions(column: Any, bucket: str) -> tuple[Any, ...]:
    year = extract("year", column).label("year")
    month = extract("month", column).label("month")
    if bucket == "day":
        day = extract("day", column).label("day")
        return (year, month, day)
    return (year, month)


def _bucket_label(row: Any, bucket: str) -> str:
    year = int(row.year)
    month = int(row.month)
    if bucket == "day":
        return f"{year:04d}-{month:02d}-{int(row.day):02d}"
    return f"{year:04d}-{month:02d}"


def _revenue_trend(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    bucket = _trend_bucket(plan)
    filtered_entities = bool(plan.service_ids or plan.branch_ids or plan.doctor_ids)
    if filtered_entities:
        expressions = _bucket_expressions(PaymentTransaction.created_at, bucket)
        stmt = (
            select(
                *expressions,
                PaymentTransaction.currency.label("currency"),
                func.coalesce(
                    func.sum(
                        case(
                            (PaymentTransaction.transaction_type == "payment", PaymentAllocation.amount_minor),
                            else_=-PaymentAllocation.amount_minor,
                        )
                    ),
                    0,
                ).label("net_minor"),
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
            .where(
                *_payment_where(workspace_id=workspace_id, plan=plan, start_at=start_at),
                *_appointment_where(
                    workspace_id=workspace_id,
                    plan=plan,
                    start_at=None,
                    include_rescheduled=True,
                ),
            )
            .group_by(*expressions, PaymentTransaction.currency)
            .order_by(*expressions, PaymentTransaction.currency)
        )
    else:
        expressions = _bucket_expressions(PaymentTransaction.created_at, bucket)
        stmt = (
            select(
                *expressions,
                PaymentTransaction.currency.label("currency"),
                func.coalesce(
                    func.sum(
                        case(
                            (PaymentTransaction.transaction_type == "payment", PaymentTransaction.amount_minor),
                            else_=-PaymentTransaction.amount_minor,
                        )
                    ),
                    0,
                ).label("net_minor"),
            )
            .where(*_payment_where(workspace_id=workspace_id, plan=plan, start_at=start_at))
            .group_by(*expressions, PaymentTransaction.currency)
            .order_by(*expressions, PaymentTransaction.currency)
        )
    raw_rows = db.execute(stmt).all()
    rows = [
        AnalyticsBIResultRow(
            label=_bucket_label(row, bucket),
            metrics=[_metric("net_paid_minor", "صافي المدفوع", int(row.net_minor or 0), currency=row.currency)],
        )
        for row in raw_rows
    ]
    return (
        f"تم تجميع صافي المدفوع على مستوى {'اليوم' if bucket == 'day' else 'الشهر'} بدون تقدير مالي.",
        [
            "صافي المدفوع = payments ناقص refunds حسب تاريخ transaction.",
            "عند فلترة خدمة/فرع/دكتور، التحليل يعتمد على payment allocations الصريحة فقط.",
        ],
        rows,
    )


def _new_patients_trend(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
    start_at: datetime | None,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    effective_created = func.coalesce(Patient.source_created_at, Patient.created_at)
    bucket = _trend_bucket(plan)
    expressions = _bucket_expressions(effective_created, bucket)
    clauses: list[Any] = [Patient.workspace_id == workspace_id]
    if start_at is not None:
        clauses.append(effective_created >= start_at)
    raw_rows = db.execute(
        select(*expressions, func.count(Patient.id).label("patients"))
        .where(*clauses)
        .group_by(*expressions)
        .order_by(*expressions)
    ).all()
    rows = [
        AnalyticsBIResultRow(
            label=_bucket_label(row, bucket),
            metrics=[_metric("new_patients", "عملاء جدد", int(row.patients or 0))],
        )
        for row in raw_rows
    ]
    return (
        f"تم تجميع العملاء الجدد على مستوى {'اليوم' if bucket == 'day' else 'الشهر'} باستخدام تاريخهم الأصلي عند توفره.",
        ["تاريخ العميل = source_created_at عند توفره، وإلا created_at في Linka."],
        rows,
    )



def _resolve_patient_for_history(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
) -> Patient:
    if plan.patient_phone:
        try:
            _display, normalized = normalize_phone(plan.patient_phone)
        except ValueError as exc:
            if not plan.patient_name:
                raise AnalyticsBIError("Patient phone in the analytics question is invalid.") from exc
            normalized = None
        if normalized:
            patient = db.scalar(
                select(Patient).where(
                    Patient.workspace_id == workspace_id,
                    Patient.phone_normalized == normalized,
                )
            )
            if patient is not None:
                return patient

    if plan.patient_name:
        name = " ".join(plan.patient_name.split()).lower()
        full_name = func.lower(
            func.trim(Patient.first_name + " " + func.coalesce(Patient.last_name, ""))
        )
        matches = list(
            db.scalars(
                select(Patient)
                .where(
                    Patient.workspace_id == workspace_id,
                    full_name == name,
                )
                .order_by(Patient.id)
                .limit(3)
            )
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AnalyticsBIError(
                "Multiple patients match that exact name. Use the phone number or open the patient in CRM."
            )

    raise AnalyticsBIError("Patient not found from the supplied phone/name.")


def _patient_history_lookup(
    db: Session,
    *,
    workspace_id: UUID,
    plan: AnalyticsBIPlan,
) -> tuple[str, list[str], list[AnalyticsBIResultRow]]:
    patient = _resolve_patient_for_history(db, workspace_id=workspace_id, plan=plan)
    context = build_patient_history_context(
        db,
        workspace_id=workspace_id,
        patient=patient,
        recent_limit=min(plan.limit, 20),
    )
    rows: list[AnalyticsBIResultRow] = [
        AnalyticsBIResultRow(
            key=str(patient.id),
            label=_full_name(patient.first_name, patient.last_name),
            secondary_label=patient.phone,
            metrics=[
                _metric("first_seen", "أول تاريخ معروف", context.profile.effective_first_seen_at.isoformat()),
                _metric("completed_visits", "زيارات مكتملة", context.completed_appointments),
                _metric("cancelled", "ملغي", context.cancelled_appointments),
                _metric("no_show", "No-show", context.no_show_appointments),
            ],
        )
    ]
    for service in context.services[: plan.limit]:
        rows.append(
            AnalyticsBIResultRow(
                key=None,
                label=service.service_name,
                secondary_label="خدمة سابقة",
                metrics=[
                    _metric("completed_visits", "جلسات مكتملة", service.completed_visits),
                    _metric(
                        "last_completed_at",
                        "آخر جلسة",
                        service.last_completed_at.isoformat() if service.last_completed_at else "—",
                    ),
                ],
            )
        )
    for money in context.money:
        rows.append(
            AnalyticsBIResultRow(
                label=f"مدفوعات {money.currency}",
                metrics=[
                    _metric("gross_paid_minor", "إجمالي الدفعات", money.gross_paid_minor, currency=money.currency),
                    _metric("refunded_minor", "Refunds", money.refunded_minor, currency=money.currency),
                    _metric("net_paid_minor", "صافي المدفوع", money.net_paid_minor, currency=money.currency),
                ],
            )
        )
    answer = (
        f"{_full_name(patient.first_name, patient.last_name)} عنده {context.completed_appointments} زيارة مكتملة"
        + (
            f"، وآخر نشاط معروف {context.last_clinic_activity_at.date().isoformat()}."
            if context.last_clinic_activity_at
            else "."
        )
    )
    return (
        answer,
        [
            "المريض يُحل بالـphone exact أولًا، أو exact unique full name فقط؛ الاسم الغامض لا يتم تخمينه.",
            "التاريخ والدفعات هنا من Linka canonical data، بما فيها البيانات القديمة المتزامنة.",
        ],
        rows,
    )

def execute_analytics_plan(
    db: Session,
    *,
    workspace_id: UUID,
    question: str,
    plan: AnalyticsBIPlan,
    model: str | None = None,
    now: datetime | None = None,
) -> AnalyticsBIAnswerRead:
    current = _now(now)
    start_at = _period_start(plan, current)

    if plan.operation == "clinic_summary":
        answer, definitions, rows = _summary(
            db, workspace_id=workspace_id, plan=plan, start_at=start_at
        )
    elif plan.operation == "appointment_outcomes":
        answer, definitions, rows = _appointment_outcomes(
            db, workspace_id=workspace_id, plan=plan, start_at=start_at
        )
    elif plan.operation == "service_performance":
        answer, definitions, rows = _entity_performance(
            db,
            workspace_id=workspace_id,
            plan=plan,
            start_at=start_at,
            entity_kind="service",
        )
    elif plan.operation == "branch_performance":
        answer, definitions, rows = _entity_performance(
            db,
            workspace_id=workspace_id,
            plan=plan,
            start_at=start_at,
            entity_kind="branch",
        )
    elif plan.operation == "doctor_performance":
        answer, definitions, rows = _entity_performance(
            db,
            workspace_id=workspace_id,
            plan=plan,
            start_at=start_at,
            entity_kind="doctor",
        )
    elif plan.operation == "service_retention":
        answer, definitions, rows = _service_retention(
            db, workspace_id=workspace_id, plan=plan, start_at=start_at
        )
    elif plan.operation == "top_repeat_patients":
        answer, definitions, rows = _top_repeat_patients(
            db, workspace_id=workspace_id, plan=plan, start_at=start_at
        )
    elif plan.operation == "lapsed_patients":
        answer, definitions, rows = _lapsed_patients(
            db,
            workspace_id=workspace_id,
            plan=plan,
            current=current,
            start_at=start_at,
        )
    elif plan.operation == "top_value_patients":
        answer, definitions, rows = _top_value_patients(
            db, workspace_id=workspace_id, plan=plan, start_at=start_at
        )
    elif plan.operation == "revenue_trend":
        answer, definitions, rows = _revenue_trend(
            db, workspace_id=workspace_id, plan=plan, start_at=start_at
        )
    elif plan.operation == "new_patients_trend":
        answer, definitions, rows = _new_patients_trend(
            db, workspace_id=workspace_id, plan=plan, start_at=start_at
        )
    elif plan.operation == "patient_history_lookup":
        answer, definitions, rows = _patient_history_lookup(
            db, workspace_id=workspace_id, plan=plan
        )
    else:  # pragma: no cover - Literal keeps this unreachable after validation.
        raise AnalyticsBIError("Unsupported analytics operation.")

    return AnalyticsBIAnswerRead(
        question=question,
        plan=plan,
        period_label=_period_label(plan),
        answer=answer,
        definitions=definitions,
        rows=rows,
        model=model,
    )
