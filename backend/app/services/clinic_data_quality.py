from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.doctor_names import split_person_name
from app.models.appointment import Appointment
from app.models.appointment_status_history import AppointmentStatusHistory
from app.models.branch import Branch
from app.models.clinic_data_issue import ClinicDataIssue
from app.models.clinic_integration import ClinicIntegration, ClinicIntegrationEntityLink
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.schemas.clinic_import import (
    NormalizedAppointmentImport,
    NormalizedPackageImport,
    NormalizedPackageUsageImport,
    NormalizedPaymentImport,
)
from app.schemas.clinic_integration import (
    ClinicDataIssueListRead,
    ClinicDataIssueRead,
    ClinicIntegrationDataQualityRead,
)
from app.schemas.crm import normalize_patient_identity_phone, normalize_phone
from app.services.payments import seed_payment_ledger_from_appointment_snapshot


class ClinicDataQualityError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_id_for_external(
    db: Session,
    *,
    workspace_id: UUID,
    entity_type: str,
    external_id: str,
) -> UUID | None:
    link = db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace_id,
            ClinicIntegrationEntityLink.entity_type == entity_type,
            ClinicIntegrationEntityLink.external_id == external_id,
        )
    )
    if link is None:
        return None
    try:
        return UUID(str(link.canonical_id))
    except (TypeError, ValueError):
        return None




def _upsert_quality_link(
    db: Session,
    *,
    workspace_id: UUID,
    entity_type: str,
    canonical_id: UUID,
    external_id: str,
) -> None:
    link = db.scalar(
        select(ClinicIntegrationEntityLink).where(
            ClinicIntegrationEntityLink.workspace_id == workspace_id,
            ClinicIntegrationEntityLink.entity_type == entity_type,
            ClinicIntegrationEntityLink.external_id == external_id,
        )
    )
    metadata = {"source": "deferred_data_issue", "ownership": "tabular_import_repair"}
    if link is None:
        db.add(
            ClinicIntegrationEntityLink(
                workspace_id=workspace_id,
                entity_type=entity_type,
                canonical_id=str(canonical_id),
                external_id=external_id,
                metadata_json=metadata,
            )
        )
        return
    if str(link.canonical_id) != str(canonical_id):
        raise ClinicDataQualityError("مرجع البيانات مرتبط بسجل مختلف بالفعل، لذلك محتاج مراجعة قبل الإصلاح.")
    link.metadata_json = metadata


def _materialize_deferred_package_payments(
    db: Session,
    *,
    issue: ClinicDataIssue,
    package: PatientPackage,
) -> int:
    context = dict(issue.source_context or {})
    raw_payments = [item for item in list(context.get("package_payments") or []) if isinstance(item, dict)]
    created = 0
    for raw in raw_payments:
        source = NormalizedPaymentImport.model_validate(raw)
        if source.package_external_id != package.external_id:
            continue
        # A payment that also points to an appointment is internally contradictory.
        # Keep that relation out of auto-repair rather than guessing how the receipt
        # should be allocated. Such a row remains eligible for a separate data issue.
        if source.appointment_external_id:
            continue
        payment_id = _canonical_id_for_external(
            db,
            workspace_id=issue.workspace_id,
            entity_type="payment",
            external_id=source.external_id,
        )
        transaction = db.get(PaymentTransaction, payment_id) if payment_id else None
        if transaction is not None:
            if (
                transaction.workspace_id != issue.workspace_id
                or transaction.transaction_type != "payment"
                or transaction.patient_id != package.patient_id
                or int(transaction.amount_minor) != int(source.amount_minor)
                or transaction.currency != source.currency
            ):
                raise ClinicDataQualityError("دفعة الباقة الموجودة لا تطابق بيانات الإصلاح المقترحة.")
            if transaction.patient_package_id is None:
                transaction.patient_package_id = package.id
            elif transaction.patient_package_id != package.id:
                raise ClinicDataQualityError("دفعة الباقة مرتبطة بباقة مختلفة بالفعل.")
        else:
            transaction = PaymentTransaction(
                workspace_id=issue.workspace_id,
                appointment_id=None,
                origin_appointment_id=None,
                patient_id=package.patient_id,
                created_by_user_id=None,
                reference_transaction_id=None,
                patient_package_id=package.id,
                transaction_type="payment",
                amount_minor=source.amount_minor,
                currency=source.currency,
                payment_method=source.payment_method,
                source="integration",
                external_reference=source.external_reference,
                reason=None,
                idempotency_key=(
                    "deferred-payment:"
                    + hashlib.sha256(
                        f"{issue.workspace_id}:{source.external_id}".encode()
                    ).hexdigest()[:48]
                ),
                created_at=source.paid_at,
            )
            db.add(transaction)
            db.flush()
            _upsert_quality_link(
                db,
                workspace_id=issue.workspace_id,
                entity_type="payment",
                canonical_id=transaction.id,
                external_id=source.external_id,
            )
            created += 1
        if package.purchase_transaction_id is None:
            package.purchase_transaction_id = transaction.id
    return created


def _summary(db: Session, workspace_id: UUID) -> ClinicIntegrationDataQualityRead:
    rows = db.execute(
        select(ClinicDataIssue.severity, func.count(ClinicDataIssue.id)).where(
            ClinicDataIssue.workspace_id == workspace_id,
            ClinicDataIssue.status == "open",
        ).group_by(ClinicDataIssue.severity)
    ).all()
    counts = {str(severity): int(count) for severity, count in rows}
    category_rows = db.execute(
        select(ClinicDataIssue.category, func.count(ClinicDataIssue.id)).where(
            ClinicDataIssue.workspace_id == workspace_id,
            ClinicDataIssue.status == "open",
        ).group_by(ClinicDataIssue.category)
    ).all()
    affected_rows = db.scalar(
        select(func.coalesce(func.sum(ClinicDataIssue.occurrence_count), 0)).where(
            ClinicDataIssue.workspace_id == workspace_id,
            ClinicDataIssue.status == "open",
        )
    ) or 0
    open_count = sum(counts.values())
    return ClinicIntegrationDataQualityRead(
        open_count=open_count,
        affected_rows=int(affected_rows),
        critical=counts.get("critical", 0),
        normal=counts.get("normal", 0),
        simple=counts.get("simple", 0),
        categories={str(category): int(count) for category, count in category_rows},
        status="attention_available" if open_count else "clean",
    )


def _sync_integration_summary(db: Session, workspace_id: UUID) -> ClinicIntegrationDataQualityRead:
    summary = _summary(db, workspace_id)
    integration = db.get(ClinicIntegration, workspace_id)
    if integration is not None:
        current = dict(integration.config_json or {})
        current["data_quality"] = summary.model_dump(mode="json")
        integration.config_json = current
    return summary


def list_data_issues(
    db: Session,
    *,
    workspace_id: UUID,
    status: str = "open",
    limit: int = 500,
) -> ClinicDataIssueListRead:
    query = select(ClinicDataIssue).where(ClinicDataIssue.workspace_id == workspace_id)
    if status != "all":
        query = query.where(ClinicDataIssue.status == status)
    issues = list(
        db.scalars(
            query.order_by(
                ClinicDataIssue.severity.asc(),
                ClinicDataIssue.created_at.asc(),
            ).limit(limit)
        )
    )
    return ClinicDataIssueListRead(
        summary=_summary(db, workspace_id),
        issues=[
            ClinicDataIssueRead(
                id=item.id,
                severity=item.severity,
                status=item.status,
                category=item.category,
                code=item.code,
                title=item.title,
                description=item.description,
                entity_type=item.entity_type,
                entity_external_id=item.entity_external_id,
                related_external_id=item.related_external_id,
                occurrence_count=item.occurrence_count,
                repair_options=list((item.source_context or {}).get("repair_options") or []),
                created_at=item.created_at,
                resolved_at=item.resolved_at,
            )
            for item in issues
        ],
    )



def _imported_operational_status(lifecycle: str) -> str:
    return {
        "scheduled": "confirmed",
        "completed": "completed",
        "cancelled": "cancelled",
        "no_show": "no_show",
        "unknown": "pending",
    }.get(lifecycle, "pending")


def _materialize_deferred_alias_appointment(
    db: Session,
    *,
    issue: ClinicDataIssue,
    raw: dict[str, Any],
) -> bool:
    """Materialize one appointment once all of its catalog aliases are known.

    Returning False means another deferred alias is still unresolved. The current
    alias issue can still be resolved; a later sibling alias resolution will retry
    the same appointment snapshot and materialize it once every catalog reference
    has a canonical link.
    """

    source = NormalizedAppointmentImport.model_validate(raw)
    service_id = _canonical_id_for_external(
        db, workspace_id=issue.workspace_id, entity_type="service", external_id=source.service_external_id
    )
    branch_id = _canonical_id_for_external(
        db, workspace_id=issue.workspace_id, entity_type="branch", external_id=source.branch_external_id
    )
    doctor_id = _canonical_id_for_external(
        db, workspace_id=issue.workspace_id, entity_type="doctor", external_id=source.doctor_external_id
    )
    if service_id is None or branch_id is None or doctor_id is None or source.start_at is None:
        return False
    service = db.get(Service, service_id)
    branch = db.get(Branch, branch_id)
    doctor = db.get(Doctor, doctor_id)
    if service is None or branch is None or doctor is None:
        return False

    patient_id = _canonical_id_for_external(
        db,
        workspace_id=issue.workspace_id,
        entity_type="patient",
        external_id=source.patient_external_id,
    )
    patient = db.get(Patient, patient_id) if patient_id else None
    display_phone = None
    normalized_phone = None
    if source.patient_phone:
        try:
            display_phone, normalized_phone = normalize_phone(source.patient_phone)
        except ValueError:
            display_phone = None
            normalized_phone = None
    if patient is None and normalized_phone:
        patient = db.scalar(
            select(Patient).where(
                Patient.workspace_id == issue.workspace_id,
                Patient.phone_normalized == normalized_phone,
            )
        )
    if patient is None:
        first_name, last_name = split_person_name(source.patient_name)
        patient = Patient(
            workspace_id=issue.workspace_id,
            first_name=first_name,
            last_name=last_name or None,
            phone=display_phone,
            phone_normalized=normalized_phone,
            source=source.source,
            source_detail="Imported from deferred data repair",
            status="active",
        )
        db.add(patient)
        db.flush()
    _upsert_quality_link(
        db,
        workspace_id=issue.workspace_id,
        entity_type="patient",
        canonical_id=patient.id,
        external_id=source.patient_external_id,
    )

    appointment_id = _canonical_id_for_external(
        db,
        workspace_id=issue.workspace_id,
        entity_type="appointment",
        external_id=source.external_id,
    )
    appointment = db.get(Appointment, appointment_id) if appointment_id else None
    start_at = source.start_at
    end_at = source.end_at or (start_at + timedelta(minutes=service.duration_minutes))
    duration = max(1, int((end_at - start_at).total_seconds() // 60))
    busy_start_at = start_at - timedelta(minutes=service.buffer_before_minutes or 0)
    busy_end_at = end_at + timedelta(minutes=service.buffer_after_minutes or 0)
    status = _imported_operational_status(source.status)
    previous_status = appointment.status if appointment is not None else None

    patient_package_id = None
    safe_billing_context = source.billing_context
    safe_payment_status = source.payment_status
    safe_amount_paid_minor = source.amount_paid_minor
    safe_payment_method = source.payment_method
    if source.billing_context == "package_prepaid":
        package_id = (
            _canonical_id_for_external(
                db,
                workspace_id=issue.workspace_id,
                entity_type="patient_package",
                external_id=source.package_external_id,
            )
            if source.package_external_id
            else None
        )
        package = db.get(PatientPackage, package_id) if package_id else None
        if package is not None and package.patient_id == patient.id and package.service_id == service.id:
            patient_package_id = package.id
            safe_payment_status = "paid"
            safe_amount_paid_minor = None
            safe_payment_method = "unknown"
        else:
            # Preserve the appointment while keeping unresolved entitlement/payment
            # attribution out of financial truth until the package issue is fixed.
            safe_billing_context = "standard"
            safe_payment_status = "unknown"
            safe_amount_paid_minor = None
            safe_payment_method = "unknown"

    if appointment is None:
        appointment = Appointment(
            workspace_id=issue.workspace_id,
            patient_id=patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            doctor_assignment_known=source.doctor_assignment_known,
            service_id=service.id,
            status=status,
            source=source.source,
            start_at=start_at,
            end_at=end_at,
            busy_start_at=busy_start_at,
            busy_end_at=busy_end_at,
            duration_minutes=duration,
            price_minor=service.price_minor,
            currency="EGP",
            payment_status=safe_payment_status,
            amount_paid_minor=safe_amount_paid_minor,
            payment_method=safe_payment_method,
            billing_context=safe_billing_context,
            package_external_id=source.package_external_id,
            patient_package_id=patient_package_id,
            idempotency_key=(
                "deferred-appointment:"
                + hashlib.sha256(f"{issue.workspace_id}:{source.external_id}".encode()).hexdigest()[:48]
            ),
        )
        db.add(appointment)
    else:
        appointment.patient_id = patient.id
        appointment.branch_id = branch.id
        appointment.doctor_id = doctor.id
        appointment.doctor_assignment_known = source.doctor_assignment_known
        appointment.service_id = service.id
        appointment.status = status
        appointment.source = source.source
        appointment.start_at = start_at
        appointment.end_at = end_at
        appointment.busy_start_at = busy_start_at
        appointment.busy_end_at = busy_end_at
        appointment.duration_minutes = duration
        appointment.price_minor = service.price_minor
        appointment.currency = "EGP"
        appointment.payment_status = safe_payment_status
        appointment.amount_paid_minor = safe_amount_paid_minor
        appointment.payment_method = safe_payment_method
        appointment.billing_context = safe_billing_context
        appointment.package_external_id = source.package_external_id
        appointment.patient_package_id = patient_package_id
    db.flush()
    _upsert_quality_link(
        db,
        workspace_id=issue.workspace_id,
        entity_type="appointment",
        canonical_id=appointment.id,
        external_id=source.external_id,
    )

    if safe_billing_context != "package_prepaid":
        seed_payment_ledger_from_appointment_snapshot(
            db,
            appointment=appointment,
            source_key=f"deferred-alias:{source.external_id}",
            payment_external_reference=source.payment_external_reference,
            refund_amount_minor=source.refund_amount_minor,
            refund_reason=source.refund_reason,
            refunded_at=source.refunded_at,
        )
    if previous_status != status:
        db.add(
            AppointmentStatusHistory(
                workspace_id=issue.workspace_id,
                appointment_id=appointment.id,
                changed_by_user_id=None,
                from_status=previous_status,
                to_status=status,
                reason="deferred_data_repair",
                metadata_json={"external_id": source.external_id, "source": "reference_alias"},
            )
        )
    return True



def _materialize_alias_dependent_usage(
    db: Session,
    *,
    issue: ClinicDataIssue,
    raw: dict[str, Any],
) -> bool:
    source = NormalizedPackageUsageImport.model_validate(raw)
    appointment_id = _canonical_id_for_external(
        db,
        workspace_id=issue.workspace_id,
        entity_type="appointment",
        external_id=source.appointment_external_id,
    )
    package_id = _canonical_id_for_external(
        db,
        workspace_id=issue.workspace_id,
        entity_type="patient_package",
        external_id=source.package_external_id,
    )
    appointment = db.get(Appointment, appointment_id) if appointment_id else None
    package = db.get(PatientPackage, package_id) if package_id else None
    if appointment is None or package is None:
        return False
    if package.patient_id != appointment.patient_id or package.service_id != appointment.service_id:
        return False
    existing = db.scalar(
        select(PackageUsage).where(
            PackageUsage.workspace_id == issue.workspace_id,
            PackageUsage.appointment_id == appointment.id,
        )
    )
    status = "released" if appointment.status in {"cancelled", "no_show"} else (
        "consumed" if appointment.status == "completed" else "reserved"
    )
    if status in {"reserved", "consumed"} and not _usage_capacity_available(
        db,
        package=package,
        appointment_id=appointment.id,
        sessions_used=source.sessions_used,
    ):
        return False
    used_at = None if status != "consumed" else (source.used_at or appointment.completed_at or appointment.end_at)
    if existing is None:
        existing = PackageUsage(
            workspace_id=issue.workspace_id,
            patient_package_id=package.id,
            appointment_id=appointment.id,
            external_id=source.external_id,
            sessions_used=source.sessions_used,
            status=status,
            used_at=used_at,
        )
        db.add(existing)
    else:
        existing.patient_package_id = package.id
        existing.sessions_used = source.sessions_used
        existing.status = status
        existing.used_at = used_at
        if source.external_id and not existing.external_id:
            existing.external_id = source.external_id
    db.flush()
    appointment.patient_package_id = package.id
    appointment.package_external_id = package.external_id
    appointment.billing_context = "package_prepaid"
    appointment.payment_status = "paid"
    appointment.amount_paid_minor = None
    appointment.payment_method = "unknown"
    if source.external_id:
        _upsert_quality_link(
            db,
            workspace_id=issue.workspace_id,
            entity_type="package_usage",
            canonical_id=existing.id,
            external_id=source.external_id,
        )
    return True


def _retry_alias_dependent_usages(db: Session, *, issue: ClinicDataIssue) -> int:
    siblings = list(
        db.scalars(
            select(ClinicDataIssue).where(
                ClinicDataIssue.workspace_id == issue.workspace_id,
                ClinicDataIssue.category == "reference_alias",
            )
        )
    )
    seen: set[tuple[str, str]] = set()
    repaired = 0
    for sibling in siblings:
        context = dict(sibling.source_context or {})
        for raw in list(context.get("dependent_package_usages") or []):
            if not isinstance(raw, dict):
                continue
            key = (
                str(raw.get("appointment_external_id") or ""),
                str(raw.get("package_external_id") or ""),
            )
            if not all(key) or key in seen:
                continue
            seen.add(key)
            if _materialize_alias_dependent_usage(db, issue=issue, raw=raw):
                repaired += 1
    return repaired


def _retry_deferred_alias_appointments(db: Session, *, issue: ClinicDataIssue) -> tuple[int, int]:
    context = dict(issue.source_context or {})
    rows = [item for item in list(context.get("deferred_appointments") or []) if isinstance(item, dict)]
    materialized = 0
    waiting = 0
    for raw in rows:
        if _materialize_deferred_alias_appointment(db, issue=issue, raw=raw):
            materialized += 1
        else:
            waiting += 1
    return materialized, waiting


def _retry_related_alias_appointments(db: Session, *, issue: ClinicDataIssue) -> tuple[int, int]:
    """Retry overlapping deferred snapshots from sibling alias issues.

    One appointment can contain an ambiguous service and doctor simultaneously.
    Resolving either alias stores its canonical external link; resolving the last
    missing alias should materialize the appointment even if that snapshot lives on
    a sibling issue that was already resolved earlier.
    """

    siblings = list(
        db.scalars(
            select(ClinicDataIssue).where(
                ClinicDataIssue.workspace_id == issue.workspace_id,
                ClinicDataIssue.category == "reference_alias",
            )
        )
    )
    seen: set[str] = set()
    materialized = 0
    waiting = 0
    for sibling in siblings:
        context = dict(sibling.source_context or {})
        for raw in list(context.get("deferred_appointments") or []):
            if not isinstance(raw, dict):
                continue
            external_id = str(raw.get("external_id") or "").strip()
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            if _canonical_id_for_external(
                db,
                workspace_id=issue.workspace_id,
                entity_type="appointment",
                external_id=external_id,
            ) is not None:
                continue
            if _materialize_deferred_alias_appointment(db, issue=issue, raw=raw):
                materialized += 1
            else:
                waiting += 1
    return materialized, waiting


def _patient_for_package_source(
    db: Session,
    *,
    issue: ClinicDataIssue,
    source: NormalizedPackageImport,
    patient_external_id: str | None = None,
) -> Patient | None:
    external_id = (patient_external_id or source.patient_external_id or "").strip()
    if external_id:
        patient_id = _canonical_id_for_external(
            db,
            workspace_id=issue.workspace_id,
            entity_type="patient",
            external_id=external_id,
        )
        patient = db.get(Patient, patient_id) if patient_id else None
        if patient is not None:
            return patient
    if not source.patient_phone:
        return None
    try:
        _display, normalized = normalize_phone(source.patient_phone)
        _identity_display, identity = normalize_patient_identity_phone(source.patient_phone)
    except ValueError:
        return None
    phone_keys = {value for value in (normalized, identity) if value}
    if not phone_keys:
        return None
    return db.scalar(
        select(Patient).where(
            Patient.workspace_id == issue.workspace_id,
            Patient.phone_normalized.in_(phone_keys),
        )
    )


def _package_from_context(
    db: Session,
    *,
    issue: ClinicDataIssue,
    patient_external_id: str | None = None,
    service_external_id: str | None = None,
) -> PatientPackage:
    context = dict(issue.source_context or {})
    package_raw = context.get("package")
    if not isinstance(package_raw, dict):
        raise ClinicDataQualityError("بيانات الباقة المؤجلة غير متاحة لإتمام الإصلاح.")
    source = NormalizedPackageImport.model_validate(package_raw)
    if service_external_id:
        source = source.model_copy(update={"service_external_id": service_external_id})
    existing = db.scalar(
        select(PatientPackage).where(
            PatientPackage.workspace_id == issue.workspace_id,
            PatientPackage.external_id == source.external_id,
        )
    )

    patient = _patient_for_package_source(
        db, issue=issue, source=source, patient_external_id=patient_external_id
    )
    service_id = _canonical_id_for_external(
        db,
        workspace_id=issue.workspace_id,
        entity_type="service",
        external_id=source.service_external_id,
    )
    service = db.get(Service, service_id) if service_id else None
    if patient is None or service is None:
        raise ClinicDataQualityError("العميل أو الخدمة المقترحة لم تعد متاحة. حدّث المشكلة وحاول مرة أخرى.")

    if existing is not None:
        if existing.patient_id != patient.id or existing.service_id != service.id:
            raise ClinicDataQualityError("الباقة الموجودة مرتبطة بعميل أو خدمة مختلفة عن الإصلاح المقترح.")
        package = existing
    else:
        package = PatientPackage(
            workspace_id=issue.workspace_id,
            patient_id=patient.id,
            service_id=service.id,
            purchase_transaction_id=None,
            created_by_user_id=None,
            external_id=source.external_id,
            name=source.name,
            sessions_purchased=source.sessions_purchased,
            sale_price_minor=source.sale_price_minor,
            standalone_session_price_minor_at_purchase=source.standalone_session_price_minor_at_purchase,
            currency=source.currency,
            purchased_at=source.purchased_at,
            expires_at=source.expires_at,
            status=source.status,
            source="integration",
            idempotency_key=(
                "deferred-package:"
                + hashlib.sha256(f"{issue.workspace_id}:{source.external_id}".encode()).hexdigest()[:48]
            ),
        )
        db.add(package)
        db.flush()
    _upsert_quality_link(
        db,
        workspace_id=issue.workspace_id,
        entity_type="patient_package",
        canonical_id=package.id,
        external_id=source.external_id,
    )
    _materialize_deferred_package_payments(db, issue=issue, package=package)
    return package


def _usage_capacity_available(
    db: Session,
    *,
    package: PatientPackage,
    appointment_id: UUID,
    sessions_used: int,
) -> bool:
    used = db.scalar(
        select(func.coalesce(func.sum(PackageUsage.sessions_used), 0)).where(
            PackageUsage.workspace_id == package.workspace_id,
            PackageUsage.patient_package_id == package.id,
            PackageUsage.appointment_id != appointment_id,
            PackageUsage.status.in_(["reserved", "consumed"]),
        )
    ) or 0
    return int(used) + sessions_used <= package.sessions_purchased


def _resolve_package_usage(
    db: Session,
    *,
    issue: ClinicDataIssue,
    package_external_id: str,
) -> None:
    context = dict(issue.source_context or {})
    appointment_raw = context.get("appointment")
    if not isinstance(appointment_raw, dict):
        raise ClinicDataQualityError("بيانات الموعد المؤجلة غير متاحة لإتمام الإصلاح.")
    appointment_external_id = str(appointment_raw.get("external_id") or "").strip()
    appointment_id = _canonical_id_for_external(
        db,
        workspace_id=issue.workspace_id,
        entity_type="appointment",
        external_id=appointment_external_id,
    )
    appointment = db.get(Appointment, appointment_id) if appointment_id else None
    package = db.scalar(
        select(PatientPackage).where(
            PatientPackage.workspace_id == issue.workspace_id,
            PatientPackage.external_id == package_external_id,
        )
    )
    if appointment is None or package is None:
        raise ClinicDataQualityError("الموعد أو الباقة المقترحة غير متاحة حاليًا.")
    if package.patient_id != appointment.patient_id or package.service_id != appointment.service_id:
        raise ClinicDataQualityError("الاختيار لم يعد متوافقًا مع العميل والخدمة في الموعد.")

    usage_raw = next(
        (
            item
            for item in list(context.get("package_usages") or [])
            if isinstance(item, dict)
            and str(item.get("appointment_external_id") or "") == appointment_external_id
        ),
        None,
    )
    source_usage = NormalizedPackageUsageImport.model_validate(usage_raw) if usage_raw else None
    existing = db.scalar(
        select(PackageUsage).where(
            PackageUsage.workspace_id == issue.workspace_id,
            PackageUsage.appointment_id == appointment.id,
        )
    )
    terminal_release = appointment.status in {"cancelled", "no_show"}
    status = "released" if terminal_release else ("consumed" if appointment.status == "completed" else "reserved")
    used_at = None if status != "consumed" else (
        (source_usage.used_at if source_usage else None) or appointment.completed_at or appointment.end_at
    )
    sessions_used = source_usage.sessions_used if source_usage else 1
    if status in {"reserved", "consumed"} and not _usage_capacity_available(
        db, package=package, appointment_id=appointment.id, sessions_used=sessions_used
    ):
        raise ClinicDataQualityError(
            "الإصلاح ده هيخلي استخدامات الباقة أكبر من عدد الجلسات المشتراة، لذلك محتاج مراجعة."
        )
    if existing is None:
        existing = PackageUsage(
            workspace_id=issue.workspace_id,
            patient_package_id=package.id,
            appointment_id=appointment.id,
            external_id=(source_usage.external_id if source_usage else None),
            sessions_used=sessions_used,
            status=status,
            used_at=used_at,
        )
        db.add(existing)
    else:
        existing.patient_package_id = package.id
        existing.sessions_used = sessions_used
        existing.status = status
        existing.used_at = used_at
    db.flush()
    if source_usage and source_usage.external_id:
        _upsert_quality_link(
            db,
            workspace_id=issue.workspace_id,
            entity_type="package_usage",
            canonical_id=existing.id,
            external_id=source_usage.external_id,
        )
    appointment.patient_package_id = package.id
    appointment.package_external_id = package.external_id
    appointment.billing_context = "package_prepaid"
    appointment.payment_status = "paid"
    appointment.amount_paid_minor = None
    appointment.payment_method = "unknown"


def _auto_resolve_package_usage_children(
    db: Session,
    *,
    issue: ClinicDataIssue,
    package: PatientPackage,
) -> int:
    children = list(
        db.scalars(
            select(ClinicDataIssue).where(
                ClinicDataIssue.workspace_id == issue.workspace_id,
                ClinicDataIssue.status == "open",
                ClinicDataIssue.category == "package_usage",
                ClinicDataIssue.entity_external_id == package.external_id,
            )
        )
    )
    compatible: list[tuple[ClinicDataIssue, Appointment]] = []
    required_sessions = 0
    for child in children:
        context = dict(child.source_context or {})
        appointment_raw = context.get("appointment")
        if not isinstance(appointment_raw, dict):
            continue
        appointment_external_id = str(appointment_raw.get("external_id") or "").strip()
        appointment_id = _canonical_id_for_external(
            db,
            workspace_id=issue.workspace_id,
            entity_type="appointment",
            external_id=appointment_external_id,
        )
        appointment = db.get(Appointment, appointment_id) if appointment_id else None
        if appointment is None:
            continue
        if package.patient_id != appointment.patient_id or package.service_id != appointment.service_id:
            continue
        usage_raw = next(
            (
                item
                for item in list(context.get("package_usages") or [])
                if isinstance(item, dict)
                and str(item.get("appointment_external_id") or "") == appointment_external_id
            ),
            None,
        )
        source_usage = NormalizedPackageUsageImport.model_validate(usage_raw) if usage_raw else None
        sessions = source_usage.sessions_used if source_usage else 1
        if appointment.status not in {"cancelled", "no_show"}:
            required_sessions += sessions
        compatible.append((child, appointment))

    existing_sessions = db.scalar(
        select(func.coalesce(func.sum(PackageUsage.sessions_used), 0)).where(
            PackageUsage.workspace_id == package.workspace_id,
            PackageUsage.patient_package_id == package.id,
            PackageUsage.status.in_(["reserved", "consumed"]),
        )
    ) or 0
    if int(existing_sessions) + required_sessions > package.sessions_purchased:
        return 0

    resolved = 0
    for child, _appointment in compatible:
        _resolve_package_usage(db, issue=child, package_external_id=package.external_id)
        child.status = "auto_resolved"
        child.resolution = {
            "kind": "package_usage_assignment",
            "package_external_id": package.external_id,
            "reason": "resolved_from_confirmed_package_owner",
        }
        child.resolved_at = _now()
        resolved += 1
    return resolved


def _propagate_package_service_choice(
    db: Session,
    *,
    issue: ClinicDataIssue,
    package_external_id: str,
    service_external_id: str,
) -> None:
    siblings = list(
        db.scalars(
            select(ClinicDataIssue).where(
                ClinicDataIssue.workspace_id == issue.workspace_id,
                ClinicDataIssue.status == "open",
                ClinicDataIssue.entity_external_id == package_external_id,
            )
        )
    )
    for sibling in siblings:
        context = dict(sibling.source_context or {})
        package_raw = context.get("package")
        if not isinstance(package_raw, dict):
            continue
        package_raw = dict(package_raw)
        package_raw["service_external_id"] = service_external_id
        context["package"] = package_raw
        sibling.source_context = context


def resolve_data_issue(
    db: Session,
    *,
    workspace_id: UUID,
    issue_id: UUID,
    option_index: int,
) -> ClinicDataIssueListRead:
    issue = db.scalar(
        select(ClinicDataIssue).where(
            ClinicDataIssue.workspace_id == workspace_id,
            ClinicDataIssue.id == issue_id,
        )
    )
    if issue is None:
        raise ClinicDataQualityError("مشكلة البيانات المطلوبة غير موجودة.")
    if issue.status != "open":
        raise ClinicDataQualityError("تم التعامل مع هذه المشكلة بالفعل.")
    options = list((issue.source_context or {}).get("repair_options") or [])
    if option_index >= len(options):
        raise ClinicDataQualityError("الاختيار المطلوب لم يعد متاحًا.")
    option = options[option_index]
    fix = option.get("fix") if isinstance(option, dict) else None
    if not isinstance(fix, dict):
        raise ClinicDataQualityError("اقتراح الإصلاح غير صالح.")
    kind = str(fix.get("kind") or "")

    if kind == "package_patient_assignment":
        patient_external_id = str(fix.get("patient_external_id") or "").strip()
        if not patient_external_id:
            raise ClinicDataQualityError("الإصلاح لا يحتوي على العميل المطلوب.")
        package = _package_from_context(db, issue=issue, patient_external_id=patient_external_id)
        child_count = _auto_resolve_package_usage_children(db, issue=issue, package=package)
        resolution = {
            "kind": kind,
            "patient_external_id": patient_external_id,
            "package_id": str(package.id),
            "auto_resolved_related_issues": child_count,
        }
    elif kind == "package_service_assignment":
        package_external_id = str(fix.get("package_external_id") or issue.entity_external_id or "").strip()
        service_external_id = str(fix.get("service_external_id") or "").strip()
        if not package_external_id or not service_external_id:
            raise ClinicDataQualityError("الإصلاح لا يحتوي على الباقة والخدمة المطلوبتين.")
        service_id = _canonical_id_for_external(
            db, workspace_id=issue.workspace_id, entity_type="service", external_id=service_external_id
        )
        if service_id is None or db.get(Service, service_id) is None:
            raise ClinicDataQualityError("الخدمة المختارة لم تعد متاحة.")
        _propagate_package_service_choice(
            db,
            issue=issue,
            package_external_id=package_external_id,
            service_external_id=service_external_id,
        )
        package: PatientPackage | None = None
        context = dict(issue.source_context or {})
        package_raw = context.get("package")
        if isinstance(package_raw, dict):
            source = NormalizedPackageImport.model_validate(package_raw).model_copy(
                update={"service_external_id": service_external_id}
            )
            if _patient_for_package_source(db, issue=issue, source=source) is not None:
                package = _package_from_context(
                    db, issue=issue, service_external_id=service_external_id
                )
        # If ownership is separately unresolved, the service decision stays on
        # sibling context and the package materializes when its owner is confirmed.
        child_count = _auto_resolve_package_usage_children(db, issue=issue, package=package) if package else 0
        resolution = {
            "kind": kind,
            "service_external_id": service_external_id,
            "package_id": str(package.id) if package else None,
            "auto_resolved_related_issues": child_count,
        }
    elif kind == "package_usage_assignment":
        package_external_id = str(fix.get("package_external_id") or "").strip()
        if not package_external_id:
            raise ClinicDataQualityError("الإصلاح لا يحتوي على الباقة المطلوبة.")
        _resolve_package_usage(db, issue=issue, package_external_id=package_external_id)
        resolution = {"kind": kind, "package_external_id": package_external_id}
    elif kind == "package_usage_exclusion":
        resolution = {"kind": kind, "excluded": True}
    elif kind == "reference_alias_assignment":
        reference_kind = str(fix.get("reference_kind") or issue.entity_type or "").strip()
        source_value = str(fix.get("source_value") or issue.entity_external_id or "").strip()
        target_external_id = str(fix.get("target_external_id") or "").strip()
        if reference_kind not in {"service", "branch", "doctor"} or not source_value or not target_external_id:
            raise ClinicDataQualityError("اختيار ربط الاسم غير مكتمل.")
        target_id = _canonical_id_for_external(
            db,
            workspace_id=issue.workspace_id,
            entity_type=reference_kind,
            external_id=target_external_id,
        )
        if target_id is None:
            raise ClinicDataQualityError("العنصر المختار لم يعد موجودًا في بيانات العيادة.")
        _upsert_quality_link(
            db,
            workspace_id=issue.workspace_id,
            entity_type=reference_kind,
            canonical_id=target_id,
            external_id=source_value,
        )
        materialized, waiting = _retry_related_alias_appointments(db, issue=issue)
        repaired_usages = _retry_alias_dependent_usages(db, issue=issue)
        resolution = {
            "kind": kind,
            "reference_kind": reference_kind,
            "source_value": source_value,
            "target_external_id": target_external_id,
            "materialized_deferred_appointments": materialized,
            "appointments_waiting_on_other_aliases": waiting,
            "materialized_dependent_package_usages": repaired_usages,
        }
    else:
        raise ClinicDataQualityError("نوع الإصلاح ده لسه محتاج مراجعة يدوية من Linka.")

    issue.status = "resolved"
    issue.resolution = resolution
    issue.resolved_at = _now()
    _sync_integration_summary(db, workspace_id)
    db.commit()
    return list_data_issues(db, workspace_id=workspace_id)
