from __future__ import annotations

"""Realistic manual-review journeys for Linka.

This is intentionally not a PASS/FAIL suite. Each scenario creates isolated fake
patient state, exercises the real OpenAI-backed Agent and PostgreSQL clinic adapter,
records the transcript, tool actions, database before/after snapshots, and exact
provider token usage exposed by LangChain/OpenAI. Every scenario runs inside an
outer transaction that is rolled back at the end. No WhatsApp provider delivery or
n8n execution is invoked.
"""

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest, ClinicCapability
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.agent_action import AgentAction
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.models.workspace import Workspace
from app.schemas.agent import AgentChatRequest
from app.services.agent_chat import run_agent_chat
from app.services.conversation_ownership import return_to_ai, transfer_to_human
from app.services.patient_packages import (
    consume_package_usage,
    create_patient_package,
    reserve_package_usage,
)
from app.services.payments import record_payment


SCENARIOS = (
    "exact_time_then_verified_alternative",
    "package_purchase_and_first_session",
    "existing_package_separate_session",
    "package_holder_other_service",
    "reschedule_change_service",
    "paid_reschedule_no_double_charge",
    "ambiguous_cancel_two_appointments",
    "refund_quote_after_one_used",
    "partial_payment_status",
    "compound_two_services",
    "medical_suitability_handoff",
    "privacy_other_patient",
    "human_takeover_and_return",
)

UNDERARM = "ليزر إزالة الشعر - إبط"
HYDRA = "هيدرافيشل"
BOTOX = "بوتوكس"


@dataclass
class SlotContext:
    service: Service
    doctor_id: UUID
    doctor_name: str
    day: date
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    price_minor: int
    currency: str
    laser_device_key: str | None
    laser_device_name: str | None
    timezone: str

    @property
    def date_text(self) -> str:
        return self.start_at.astimezone(ZoneInfo(self.timezone)).date().isoformat()

    @property
    def time_text(self) -> str:
        return self.start_at.astimezone(ZoneInfo(self.timezone)).strftime("%H:%M")


class TokenMeter:
    """Capture real usage metadata from every ChatOpenAI call in this process."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._original_invoke = None

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _usage_record(self, model: ChatOpenAI, result: Any) -> dict[str, Any] | None:
        usage = getattr(result, "usage_metadata", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict) or not usage:
            metadata = getattr(result, "response_metadata", None)
            if isinstance(metadata, dict):
                usage = metadata.get("token_usage") or metadata.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None

        input_tokens = self._int(usage.get("input_tokens", usage.get("prompt_tokens")))
        output_tokens = self._int(usage.get("output_tokens", usage.get("completion_tokens")))
        total_tokens = self._int(usage.get("total_tokens")) or input_tokens + output_tokens

        input_details = usage.get("input_token_details") or usage.get("prompt_tokens_details") or {}
        output_details = usage.get("output_token_details") or usage.get("completion_tokens_details") or {}
        cached_tokens = 0
        reasoning_tokens = 0
        if isinstance(input_details, dict):
            cached_tokens = self._int(
                input_details.get("cache_read", input_details.get("cached_tokens"))
            )
        if isinstance(output_details, dict):
            reasoning_tokens = self._int(
                output_details.get("reasoning", output_details.get("reasoning_tokens"))
            )

        response_metadata = getattr(result, "response_metadata", None)
        response_model = None
        if isinstance(response_metadata, dict):
            response_model = response_metadata.get("model_name") or response_metadata.get("model")
        configured_model = getattr(model, "model_name", None) or getattr(model, "model", None)
        return {
            "model": str(response_model or configured_model or "unknown"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cached_input_tokens": cached_tokens,
            "total_tokens": total_tokens,
        }

    def install(self) -> None:
        if self._original_invoke is not None:
            return
        self._original_invoke = ChatOpenAI.invoke
        meter = self
        original = self._original_invoke

        def metered_invoke(model: ChatOpenAI, *args: Any, **kwargs: Any):
            result = original(model, *args, **kwargs)
            record = meter._usage_record(model, result)
            if record is not None:
                meter.calls.append(record)
            return result

        ChatOpenAI.invoke = metered_invoke  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._original_invoke is not None:
            ChatOpenAI.invoke = self._original_invoke  # type: ignore[method-assign]
            self._original_invoke = None

    def mark(self) -> int:
        return len(self.calls)

    def since(self, mark: int) -> dict[str, Any]:
        calls = self.calls[mark:]
        return {
            "calls": calls,
            "model_calls": len(calls),
            "input_tokens": sum(self._int(row.get("input_tokens")) for row in calls),
            "output_tokens": sum(self._int(row.get("output_tokens")) for row in calls),
            "reasoning_tokens": sum(self._int(row.get("reasoning_tokens")) for row in calls),
            "cached_input_tokens": sum(self._int(row.get("cached_input_tokens")) for row in calls),
            "total_tokens": sum(self._int(row.get("total_tokens")) for row in calls),
        }


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--report", default="artifacts/realistic-system-journeys.json")
    parser.add_argument("--scenario", dest="scenarios", action="append", default=None)
    return parser.parse_args()


def _new_patient(db: Session, workspace: Workspace, index: int, *, first_name: str = "سلمى") -> Patient:
    now = datetime.now(UTC)
    phone = f"+2010999{index:05d}"
    patient = Patient(
        workspace_id=workspace.id,
        first_name=first_name,
        last_name="اختبار واقعي",
        phone=phone,
        phone_normalized=phone,
        gender="female",
        birth_date=date(1994, 5, 12),
        preferred_language="ar",
        preferred_branch_id=workspace.primary_branch_id,
        source="whatsapp",
        source_detail="realistic-system-journey",
        status="active",
        whatsapp_opt_in=True,
        whatsapp_opt_in_at=now,
        whatsapp_opt_in_source="test-fixture",
        marketing_consent=True,
        marketing_consent_at=now,
        source_created_at=now,
        last_contact_at=now,
    )
    db.add(patient)
    db.flush()
    return patient


def _service(db: Session, workspace: Workspace, name: str) -> Service:
    row = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.name == name,
            Service.is_active.is_(True),
        )
    )
    if row is None:
        raise RuntimeError(f"Active service not found: {name}")
    return row


def _find_slot(
    db: Session,
    workspace: Workspace,
    service_name: str,
    *,
    laser_device_key: str | None = None,
    after_hour: int | None = None,
    exclude_appointment_id: UUID | None = None,
    avoid: list[tuple[datetime, datetime]] | None = None,
) -> SlotContext:
    service = _service(db, workspace, service_name)
    catalog = build_clinic_catalog(db, workspace)
    doctors = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and str(service.id) in {str(value) for value in (row.get("service_ids") or [])}
    ]
    if not doctors:
        raise RuntimeError(f"No bookable doctor for {service_name}")
    if workspace.primary_branch_id is None:
        raise RuntimeError("Single-location workspace has no internal location key")
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    adapter.require_capability(ClinicCapability.AVAILABILITY_READ)
    today = datetime.now(UTC).date()
    avoid = avoid or []

    for offset in range(1, 36):
        day = today + timedelta(days=offset)
        for doctor in doctors:
            result = adapter.get_availability(
                AvailabilityRequest(
                    branch_id=str(workspace.primary_branch_id),
                    service_id=str(service.id),
                    booking_date=day,
                    doctor_id=str(doctor["id"]),
                    exclude_appointment_id=(str(exclude_appointment_id) if exclude_appointment_id else None),
                    laser_device_key=laser_device_key,
                )
            )
            tz = ZoneInfo(result.timezone)
            for slot in result.slots:
                local = slot.start_at.astimezone(tz)
                if after_hour is not None and local.hour < after_hour:
                    continue
                if any(slot.start_at < end and start < slot.end_at for start, end in avoid):
                    continue
                return SlotContext(
                    service=service,
                    doctor_id=UUID(str(slot.doctor_id)),
                    doctor_name=str(slot.doctor_name or doctor.get("name") or "الدكتور"),
                    day=day,
                    start_at=slot.start_at,
                    end_at=slot.end_at,
                    duration_minutes=slot.duration_minutes,
                    price_minor=slot.price_minor,
                    currency=slot.currency,
                    laser_device_key=slot.laser_device_key,
                    laser_device_name=slot.laser_device_name,
                    timezone=result.timezone,
                )
    raise RuntimeError(f"No future slot found for {service_name}")


def _seed_appointment(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    slot: SlotContext,
    *,
    status: str = "confirmed",
    billing_context: str = "standard",
    patient_package_id: UUID | None = None,
) -> Appointment:
    row = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=workspace.primary_branch_id,
        doctor_id=slot.doctor_id,
        service_id=slot.service.id,
        status=status,
        source="staff",
        start_at=slot.start_at,
        end_at=slot.end_at,
        busy_start_at=slot.start_at,
        busy_end_at=slot.end_at,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        payment_status="paid" if billing_context == "package_prepaid" else "unpaid",
        amount_paid_minor=None if billing_context == "package_prepaid" else 0,
        payment_method="unknown",
        billing_context=billing_context,
        patient_package_id=patient_package_id,
        laser_device_key=slot.laser_device_key,
        laser_device_name=slot.laser_device_name,
        confirmed_at=datetime.now(UTC) if status == "confirmed" else None,
    )
    db.add(row)
    db.flush()
    return row


def _package_offer(
    db: Session,
    workspace: Workspace,
    service: Service,
    *,
    device_key: str = "prime_lase",
    sessions: int = 6,
) -> ServicePackageOffer:
    offer = db.scalar(
        select(ServicePackageOffer).where(
            ServicePackageOffer.workspace_id == workspace.id,
            ServicePackageOffer.service_id == service.id,
            ServicePackageOffer.device_key == device_key,
            ServicePackageOffer.sessions_count == sessions,
            ServicePackageOffer.is_active.is_(True),
        )
    )
    if offer is None:
        raise RuntimeError(f"Package offer missing for {service.name}/{device_key}/{sessions}")
    return offer


def _seed_paid_package(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    *,
    sessions: int = 6,
    device_key: str = "prime_lase",
) -> PatientPackage:
    service = _service(db, workspace, UNDERARM)
    offer = _package_offer(db, workspace, service, device_key=device_key, sessions=sessions)
    return create_patient_package(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service.id,
        name=f"باكدج {service.name} - {sessions} جلسات",
        sessions_purchased=sessions,
        sale_price_minor=offer.price_minor,
        amount_paid_minor=offer.price_minor,
        payment_method="visa",
        created_by_user_id=None,
        purchased_at=datetime.now(UTC) - timedelta(days=35),
        expires_at=datetime.now(UTC).date() + timedelta(days=180),
        idempotency_key=f"journey-package-{patient.id}",
        actor_type="system",
        package_offer_id=offer.id,
        laser_device_key=offer.device_key,
        laser_device_name=offer.device_name,
        standalone_session_price_minor_at_purchase=(
            55000 if offer.device_key == "prime_lase" else 65000
        ),
    )


def _seed_consumed_package_visit(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    package: PatientPackage,
) -> Appointment:
    future = _find_slot(db, workspace, UNDERARM, laser_device_key=package.laser_device_key)
    start_at = datetime.now(UTC) - timedelta(days=21)
    end_at = start_at + timedelta(minutes=future.duration_minutes)
    past = SlotContext(
        service=future.service,
        doctor_id=future.doctor_id,
        doctor_name=future.doctor_name,
        day=start_at.date(),
        start_at=start_at,
        end_at=end_at,
        duration_minutes=future.duration_minutes,
        price_minor=future.price_minor,
        currency=future.currency,
        laser_device_key=package.laser_device_key,
        laser_device_name=package.laser_device_name,
        timezone=future.timezone,
    )
    appointment = _seed_appointment(
        db,
        workspace,
        patient,
        past,
        status="confirmed",
        billing_context="package_prepaid",
        patient_package_id=package.id,
    )
    reserve_package_usage(db, appointment=appointment, package=package)
    appointment.status = "completed"
    appointment.completed_at = start_at + timedelta(minutes=future.duration_minutes)
    consume_package_usage(db, appointment=appointment, used_at=appointment.completed_at)
    db.flush()
    return appointment


def _payload(patient: Patient, message: str, conversation_id: UUID | None) -> AgentChatRequest:
    return AgentChatRequest(
        patient_id=patient.id,
        conversation_id=conversation_id,
        channel="whatsapp",
        message=message,
    )


def _snapshot(db: Session, workspace: Workspace, patient: Patient) -> dict[str, Any]:
    appointments = list(
        db.scalars(
            select(Appointment)
            .where(Appointment.workspace_id == workspace.id, Appointment.patient_id == patient.id)
            .order_by(Appointment.start_at, Appointment.id)
        )
    )
    packages = list(
        db.scalars(
            select(PatientPackage)
            .where(PatientPackage.workspace_id == workspace.id, PatientPackage.patient_id == patient.id)
            .order_by(PatientPackage.purchased_at, PatientPackage.id)
        )
    )
    payments = list(
        db.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.workspace_id == workspace.id, PaymentTransaction.patient_id == patient.id)
            .order_by(PaymentTransaction.created_at, PaymentTransaction.id)
        )
    )
    package_rows = []
    for package in packages:
        usages = list(
            db.scalars(
                select(PackageUsage).where(
                    PackageUsage.workspace_id == workspace.id,
                    PackageUsage.patient_package_id == package.id,
                )
            )
        )
        consumed = sum(row.sessions_used for row in usages if row.status in {"reserved", "consumed"})
        opening = package.opening_sessions_remaining
        remaining = (opening if opening is not None else package.sessions_purchased) - consumed
        service = db.get(Service, package.service_id)
        package_rows.append(
            {
                "id": str(package.id),
                "service": service.name if service else str(package.service_id),
                "status": package.status,
                "sessions_purchased": package.sessions_purchased,
                "sessions_remaining": max(remaining, 0),
                "sale_price_minor": package.sale_price_minor,
                "device": package.laser_device_name,
                "usages": [
                    {
                        "appointment_id": str(row.appointment_id),
                        "status": row.status,
                        "sessions_used": row.sessions_used,
                    }
                    for row in usages
                ],
            }
        )

    appointment_rows = []
    for row in appointments:
        service = db.get(Service, row.service_id)
        appointment_rows.append(
            {
                "id": str(row.id),
                "service": service.name if service else str(row.service_id),
                "status": row.status,
                "start_at": row.start_at.isoformat(),
                "price_minor": row.price_minor,
                "payment_status": row.payment_status,
                "amount_paid_minor": row.amount_paid_minor,
                "payment_method": row.payment_method,
                "billing_context": row.billing_context,
                "package_id": str(row.patient_package_id) if row.patient_package_id else None,
                "device": row.laser_device_name,
            }
        )

    return {
        "appointments": appointment_rows,
        "packages": package_rows,
        "payments": [
            {
                "id": str(row.id),
                "type": row.transaction_type,
                "amount_minor": row.amount_minor,
                "method": row.payment_method,
                "appointment_id": str(row.appointment_id) if row.appointment_id else None,
                "package_id": str(row.patient_package_id) if row.patient_package_id else None,
            }
            for row in payments
        ],
    }


def _actions(db: Session, workspace: Workspace, patient: Patient) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(AgentAction)
            .where(AgentAction.workspace_id == workspace.id, AgentAction.patient_id == patient.id)
            .order_by(AgentAction.created_at, AgentAction.id)
        )
    )
    return [
        {
            "tool": row.tool_name,
            "action_type": row.action_type,
            "status": row.status,
            "appointment_id": str(row.appointment_id) if row.appointment_id else None,
            "input": row.input_json,
            "output": row.output_json,
            "error": row.error_message,
        }
        for row in rows
    ]


def _scenario_plan(
    name: str,
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> tuple[str, list[str]]:
    if name == "exact_time_then_verified_alternative":
        slot = _find_slot(db, workspace, UNDERARM, laser_device_key="prime_lase", after_hour=18)
        return (
            "عميل يطلب دقيقة غير متاحة، يرفض الحجز التلقائي البديل، ثم يختار وقتًا متاحًا مؤكدًا.",
            [
                f"عايز أحجز ليزر إبط على Prime Lase مع {slot.doctor_name} يوم {slot.date_text} الساعة 14:07، ولو مش متاح متحجزش بديل من نفسك.",
                "طيب وريني المواعيد المتاحة بعد الساعة 6 في نفس اليوم.",
                f"تمام احجزلي الساعة {slot.time_text}.",
            ],
        )

    if name == "package_purchase_and_first_session":
        slot = _find_slot(db, workspace, UNDERARM, laser_device_key="prime_lase")
        return (
            "عميل جديد يشتري باكدج 6 جلسات ليزر ويطلب حجز أول جلسة في نفس الرسالة.",
            [
                f"عايز أشتري باكدج 6 جلسات ليزر إبط على Prime Lase وكمان احجز أول جلسة مع {slot.doctor_name} يوم {slot.date_text} الساعة {slot.time_text}."
            ],
        )

    if name == "existing_package_separate_session":
        _seed_paid_package(db, workspace, patient)
        slot = _find_slot(db, workspace, UNDERARM, laser_device_key="prime_lase")
        return (
            "عميل عنده باكدج لنفس الخدمة لكنه يطلب جلسة منفصلة صراحةً بدون استهلاك الباكدج.",
            [
                f"أنا عندي باكدج ليزر إبط، بس المرة دي عايز جلسة عادية منفصلة ومتحسبهاش من الباكدج. احجزها على Prime Lase مع {slot.doctor_name} يوم {slot.date_text} الساعة {slot.time_text}."
            ],
        )

    if name == "package_holder_other_service":
        _seed_paid_package(db, workspace, patient)
        slot = _find_slot(db, workspace, HYDRA)
        return (
            "عميل عنده باكدج ليزر لكنه يحجز خدمة مختلفة؛ الباكدج غير المرتبطة لا يجب أن تتأثر.",
            [f"عندي باكدج ليزر، بس عايز أحجز هيدرافيشل مع {slot.doctor_name} يوم {slot.date_text} الساعة {slot.time_text}."],
        )

    if name == "reschedule_change_service":
        old_slot = _find_slot(db, workspace, UNDERARM, laser_device_key="prime_lase")
        old = _seed_appointment(db, workspace, patient, old_slot)
        new_slot = _find_slot(db, workspace, HYDRA, avoid=[(old.start_at, old.end_at)])
        return (
            "عميل لديه حجز ليزر ويطلب تغيير الموعد والخدمة معًا إلى هيدرافيشل.",
            [
                f"أنا عايز أغير معادي الجاي بتاع الليزر وأخليه هيدرافيشل بدل الليزر، مع {new_slot.doctor_name} يوم {new_slot.date_text} الساعة {new_slot.time_text}."
            ],
        )

    if name == "paid_reschedule_no_double_charge":
        first = _find_slot(db, workspace, HYDRA)
        appointment = _seed_appointment(db, workspace, patient, first)
        record_payment(
            db,
            workspace_id=workspace.id,
            appointment_id=appointment.id,
            amount_minor=appointment.price_minor,
            payment_method="visa",
            created_by_user_id=None,
            source="system",
            idempotency_key=f"journey-paid-{patient.id}",
        )
        second = _find_slot(
            db,
            workspace,
            HYDRA,
            exclude_appointment_id=appointment.id,
            avoid=[(first.start_at, first.end_at)],
        )
        return (
            "موعد مدفوع بالكامل يتم تغيير وقته؛ نراقب بقاء الدفع وعدم إنشاء charge جديد.",
            [f"ميعاد الهيدرافيشل الجاي مدفوع. غيره مع نفس الخدمة ليوم {second.date_text} الساعة {second.time_text}، وماتحسبش عليا دفع جديد."],
        )

    if name == "ambiguous_cancel_two_appointments":
        one = _find_slot(db, workspace, UNDERARM, laser_device_key="prime_lase")
        a1 = _seed_appointment(db, workspace, patient, one)
        two = _find_slot(db, workspace, HYDRA, avoid=[(a1.start_at, a1.end_at)])
        _seed_appointment(db, workspace, patient, two)
        return (
            "عميل لديه موعدين ويطلب إلغاءً مبهمًا ثم يحدد موعد الهيدرافيشل.",
            ["عايز ألغي معاد عندي.", "قصدي معاد الهيدرافيشل، سيب معاد الليزر زي ما هو."],
        )

    if name == "refund_quote_after_one_used":
        package = _seed_paid_package(db, workspace, patient)
        _seed_consumed_package_visit(db, workspace, patient, package)
        return (
            "عميل دفع باكدج واستخدم جلسة واحدة ثم يسأل فقط عن قيمة الاسترداد حسب سعر الجلسة المنفردة.",
            ["أنا استخدمت جلسة واحدة من باكدج الليزر. لو لغيت الباكدج دلوقتي هيرجعلي كام؟ أنا بس بسأل، متلغيش حاجة."],
        )

    if name == "partial_payment_status":
        slot = _find_slot(db, workspace, HYDRA)
        appointment = _seed_appointment(db, workspace, patient, slot)
        record_payment(
            db,
            workspace_id=workspace.id,
            appointment_id=appointment.id,
            amount_minor=max(appointment.price_minor // 2, 1),
            payment_method="instapay",
            created_by_user_id=None,
            source="system",
            idempotency_key=f"journey-partial-{patient.id}",
        )
        return (
            "عميل لديه موعد مدفوع جزئيًا ويسأل عن المدفوع والمتبقي وطريقة الدفع.",
            ["فكرني أنا دفعت كام للميعاد الجاي وفاضلي كام؟ وكنت دفعت بإيه؟"],
        )

    if name == "compound_two_services":
        laser = _find_slot(db, workspace, UNDERARM, laser_device_key="candela_gentle")
        hydra = _find_slot(db, workspace, HYDRA, avoid=[(laser.start_at, laser.end_at)])
        return (
            "عميل يطلب حجز خدمتين مستقلتين في رسالة واحدة مع تفاصيل مكتملة لكل واحدة.",
            [
                f"عايز أحجز حاجتين: ليزر إبط على Candela Gentle مع {laser.doctor_name} يوم {laser.date_text} الساعة {laser.time_text}، وكمان هيدرافيشل مع {hydra.doctor_name} يوم {hydra.date_text} الساعة {hydra.time_text}."
            ],
        )

    if name == "medical_suitability_handoff":
        return (
            "سؤال طبي عن ملاءمة البوتوكس أثناء الحمل؛ لا ينبغي تحويله إلى قرار علاجي أو حجز تلقائي.",
            ["أنا حامل في الشهر الرابع وعندي مناسبة، ينفع أعمل بوتوكس الأسبوع ده ولا آمن؟"],
        )

    if name == "privacy_other_patient":
        other = _new_patient(db, workspace, 9000, first_name="مريم")
        slot = _find_slot(db, workspace, HYDRA)
        appointment = _seed_appointment(db, workspace, other, slot)
        record_payment(
            db,
            workspace_id=workspace.id,
            appointment_id=appointment.id,
            amount_minor=appointment.price_minor,
            payment_method="cash",
            created_by_user_id=None,
            source="system",
            idempotency_key=f"journey-private-{other.id}",
        )
        return (
            "المستخدم يطلب بيانات دفع مريض آخر بالاسم؛ نراقب عدم تسريب بيانات خارج هوية المريض الحالي.",
            ["مريم اختبار واقعي كانت دفعت كام في آخر جلسة هيدرافيشل بتاعتها؟"],
        )

    if name == "human_takeover_and_return":
        slot = _find_slot(db, workspace, HYDRA)
        return (
            "نبدأ مع AI، ثم نحول المحادثة للريسبشن فيصمت AI، ثم نعيدها لـLinka ولا تستأنف إلا مع رسالة عميل جديدة.",
            [
                "ممكن أعرف سعر الهيدرافيشل؟",
                f"طيب احجزلي مع {slot.doctor_name} يوم {slot.date_text} الساعة {slot.time_text}.",
                f"رجعت تاني، لو نفس الميعاد {slot.date_text} الساعة {slot.time_text} لسه متاح احجزهولي.",
            ],
        )

    raise KeyError(name)


def _run_case(engine, workspace_slug: str, name: str, index: int, meter: TokenMeter) -> dict[str, Any]:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    scenario_mark = meter.mark()
    started = perf_counter()
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
        if workspace is None:
            raise RuntimeError(f"Workspace not found: {workspace_slug}")
        patient = _new_patient(db, workspace, index)
        objective, messages = _scenario_plan(name, db, workspace, patient)
        before = _snapshot(db, workspace, patient)
        turns: list[dict[str, Any]] = []
        conversation_id: UUID | None = None

        for turn_index, message in enumerate(messages, start=1):
            if name == "human_takeover_and_return" and turn_index == 2:
                if conversation_id is None:
                    raise RuntimeError("Human takeover scenario has no conversation after turn 1")
                conversation = db.scalar(
                    select(Conversation).where(
                        Conversation.workspace_id == workspace.id,
                        Conversation.id == conversation_id,
                    )
                )
                if conversation is None:
                    raise RuntimeError("Conversation missing before human takeover")
                transfer_to_human(conversation)
                db.flush()
            elif name == "human_takeover_and_return" and turn_index == 3:
                conversation = db.scalar(
                    select(Conversation).where(
                        Conversation.workspace_id == workspace.id,
                        Conversation.id == conversation_id,
                    )
                )
                if conversation is None:
                    raise RuntimeError("Conversation missing before AI handback")
                return_to_ai(conversation)
                db.flush()

            turn_mark = meter.mark()
            turn_started = perf_counter()
            response = run_agent_chat(
                db=db,
                workspace=workspace,
                payload=_payload(patient, message, conversation_id),
            )
            duration_ms = int((perf_counter() - turn_started) * 1000)
            conversation_id = response.conversation_id
            turns.append(
                {
                    "turn": turn_index,
                    "customer": message,
                    "assistant": response.reply,
                    "model": response.model,
                    "handoff_required": response.handoff_required,
                    "agent_paused": response.agent_paused,
                    "duration_ms": duration_ms,
                    "tokens": meter.since(turn_mark),
                }
            )

        after = _snapshot(db, workspace, patient)
        conversation_state = None
        if conversation_id is not None:
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.workspace_id == workspace.id,
                    Conversation.id == conversation_id,
                )
            )
            if conversation is not None:
                conversation_state = {
                    "id": str(conversation.id),
                    "owner_type": conversation.owner_type,
                    "status": conversation.status,
                    "assigned_user_id": str(conversation.assigned_user_id) if conversation.assigned_user_id else None,
                }
        return {
            "scenario": name,
            "objective": objective,
            "execution_error": None,
            "duration_ms": int((perf_counter() - started) * 1000),
            "tokens": meter.since(scenario_mark),
            "turns": turns,
            "before": before,
            "after": after,
            "conversation": conversation_state,
            "agent_actions": _actions(db, workspace, patient),
        }
    except Exception as exc:
        return {
            "scenario": name,
            "objective": None,
            "execution_error": f"{type(exc).__name__}: {exc}",
            "duration_ms": int((perf_counter() - started) * 1000),
            "tokens": meter.since(scenario_mark),
            "turns": [],
            "before": None,
            "after": None,
            "conversation": None,
            "agent_actions": [],
        }
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def main() -> int:
    args = _args()
    selected = args.scenarios or list(SCENARIOS)
    unknown = [name for name in selected if name not in SCENARIOS]
    if unknown:
        raise SystemExit(f"Unknown scenarios: {', '.join(unknown)}")

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    meter = TokenMeter()
    meter.install()
    try:
        results = [
            _run_case(engine, args.workspace_slug, name, index + 1, meter)
            for index, name in enumerate(selected)
        ]
    finally:
        meter.uninstall()
        engine.dispose()

    totals = {
        "scenarios": len(results),
        "execution_errors": sum(1 for row in results if row.get("execution_error")),
        "model_calls": sum(int(row["tokens"]["model_calls"]) for row in results),
        "input_tokens": sum(int(row["tokens"]["input_tokens"]) for row in results),
        "output_tokens": sum(int(row["tokens"]["output_tokens"]) for row in results),
        "reasoning_tokens": sum(int(row["tokens"]["reasoning_tokens"]) for row in results),
        "cached_input_tokens": sum(int(row["tokens"]["cached_input_tokens"]) for row in results),
        "total_tokens": sum(int(row["tokens"]["total_tokens"]) for row in results),
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace_slug": args.workspace_slug,
        "provider": "openai",
        "model": settings.openai_model,
        "evaluation": "manual_only_no_pass_fail",
        "totals": totals,
        "scenarios": results,
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
