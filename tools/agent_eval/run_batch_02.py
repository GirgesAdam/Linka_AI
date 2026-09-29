from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.conversation import Conversation
from app.models.doctor_service import DoctorService
from app.models.message import Message
from app.models.patient import Patient
from app.models.patient_package import PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.state_persistence import load_active_task
from app.services.pulse_billing import (
    list_patient_pulse_balances,
    list_pulse_billing_settings,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    acquire_eval_advisory_lock,
    active_branch_id,
    assert_demo_only,
    batch_token_summary,
    booking_context,
    default_evaluation,
    jsonable,
    local_slot,
    money,
    send_turn,
    service_by_slug,
    state_snapshot,
)
from tools.agent_eval.run_batch_01 import (
    context_with_two_doctors,
    created_appointments,
    doctor_name,
    quiet_patient,
)
from tools.agent_eval.run_pulse_domain import (
    _active_offer_for_device,
    _active_patient,
    _patient_with_pulse_balance,
)

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
BATCH_NUMBER = 2
SCENARIO_VERSION = "batch2-v1"
NATIVE_HISTORY_LIMIT = 6
BATCH2_FIXTURE_VERSION = "batch2-demo-fixtures-v1"


def _ensure_batch2_catalog_fixtures(
    db: Session,
    workspace: Workspace,
) -> dict[str, str]:
    """Seed only missing Batch 2 catalog rows inside the scenario rollback."""

    baseline = build_clinic_catalog(db, workspace)
    doctor_ids = [
        UUID(str(row["id"]))
        for row in baseline.get("doctors", [])
        if isinstance(row, dict) and row.get("id")
    ][:3]
    if not doctor_ids:
        raise RuntimeError("EVAL_INFRA_ERROR: no bookable doctors for Batch 2 fixtures")

    specs = (
        {
            "slug": "hydrafacial",
            "name": "Hydrafacial",
            "category": "Facial",
            "operational_category": "dermatology",
            "duration_minutes": 60,
            "price_minor": 180000,
            "requires_laser_device": False,
        },
        {
            "slug": "laser-hair-removal-underarm",
            "name": "ليزر إزالة الشعر - إبط",
            "category": "Laser Hair Removal",
            "operational_category": "laser",
            "duration_minutes": 15,
            "price_minor": 55000,
            "requires_laser_device": True,
        },
    )
    service_ids: dict[str, str] = {}
    for spec in specs:
        service = db.scalar(
            select(Service).where(
                Service.workspace_id == workspace.id,
                Service.slug == spec["slug"],
            )
        )
        if service is None:
            service = Service(
                workspace_id=workspace.id,
                name=str(spec["name"]),
                slug=str(spec["slug"]),
                category=str(spec["category"]),
                operational_category=str(spec["operational_category"]),
                duration_minutes=int(spec["duration_minutes"]),
                price_minor=int(spec["price_minor"]),
                currency="EGP",
                requires_medical_review=False,
                requires_laser_device=bool(spec["requires_laser_device"]),
                is_active=True,
            )
            db.add(service)
            db.flush()
        service_ids[str(spec["slug"])] = str(service.id)

        for doctor_id in doctor_ids:
            exists = db.scalar(
                select(DoctorService.id).where(
                    DoctorService.workspace_id == workspace.id,
                    DoctorService.doctor_id == doctor_id,
                    DoctorService.service_id == service.id,
                    DoctorService.is_active.is_(True),
                )
            )
            if exists is None:
                db.add(
                    DoctorService(
                        workspace_id=workspace.id,
                        doctor_id=doctor_id,
                        service_id=service.id,
                        is_active=True,
                    )
                )

        if spec["slug"] == "laser-hair-removal-underarm":
            for device_key, device_name, price_minor in (
                ("prime_lase", "Prime Lase", 55000),
                ("candela_gentle", "Candela Gentle", 70000),
            ):
                price = db.scalar(
                    select(ServiceDevicePrice).where(
                        ServiceDevicePrice.workspace_id == workspace.id,
                        ServiceDevicePrice.service_id == service.id,
                        ServiceDevicePrice.device_key == device_key,
                        ServiceDevicePrice.is_active.is_(True),
                    )
                )
                if price is None:
                    db.add(
                        ServiceDevicePrice(
                            workspace_id=workspace.id,
                            service_id=service.id,
                            device_key=device_key,
                            device_name=device_name,
                            price_minor=price_minor,
                            duration_minutes=15,
                            currency="EGP",
                            is_active=True,
                        )
                    )
    db.flush()
    return service_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--output-dir", default="backend/eval_results")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA") or "unknown")
    parser.add_argument("--batch2-base-sha", required=True)
    parser.add_argument("--input-price-per-million", type=float, required=True)
    parser.add_argument("--cached-input-price-per-million", type=float, required=True)
    parser.add_argument("--output-price-per-million", type=float, required=True)
    parser.add_argument("--cache-write-multiplier", type=float, default=1.0)
    parser.add_argument("--pricing-source", required=True)
    return parser.parse_args()


def require_explicit_demo_eval() -> None:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError(
            "Set TIA_AGENT_EVAL_CONFIRM_DEMO=1 to run the live Demo evaluation."
        )


def run_messages(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    scenario_id: str,
    messages: list[str],
    *,
    conversation_id: UUID | None = None,
):
    turns = []
    current = conversation_id
    for index, message in enumerate(messages, start=1):
        response, turn = send_turn(
            db,
            workspace,
            patient,
            scenario_id,
            index,
            message,
            current,
        )
        current = response.conversation_id
        turns.append(turn)
        if response.agent_paused:
            break
    return turns, current


def db_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    collections = (
        "appointments",
        "packages",
        "pulse_packs",
        "payments",
        "pulse_usages",
        "pulse_settlements",
    )
    output: dict[str, Any] = {}
    for key in collections:
        left = before.get(key) or []
        right = after.get(key) or []
        left_by_id = {str(row["id"]): row for row in left if row.get("id")}
        right_by_id = {str(row["id"]): row for row in right if row.get("id")}
        output[key] = {
            "created": sorted(set(right_by_id) - set(left_by_id)),
            "removed": sorted(set(left_by_id) - set(right_by_id)),
            "changed": sorted(
                row_id
                for row_id in set(left_by_id) & set(right_by_id)
                if left_by_id[row_id] != right_by_id[row_id]
            ),
        }

    before_balances = {
        str(row.get("device_key")): int(row.get("pulses_remaining") or 0)
        for row in before.get("pulse_balances") or []
    }
    after_balances = {
        str(row.get("device_key")): int(row.get("pulses_remaining") or 0)
        for row in after.get("pulse_balances") or []
    }
    output["pulse_balance_delta"] = {
        key: after_balances.get(key, 0) - before_balances.get(key, 0)
        for key in sorted(set(before_balances) | set(after_balances))
        if after_balances.get(key, 0) != before_balances.get(key, 0)
    }
    return output


def _structured_ops(turn) -> list[dict[str, Any]]:
    if not turn.structured_trace:
        return []
    understanding = turn.structured_trace[-1].get("understanding") or {}
    operations = understanding.get("operations")
    return list(operations) if isinstance(operations, list) else []


def _active_task(turn) -> dict[str, Any] | None:
    if not turn.structured_trace:
        return None
    value = turn.structured_trace[-1].get("active_task")
    return dict(value) if isinstance(value, dict) else None


def _observed(turns) -> dict[str, Any]:
    return {
        "turns": len(turns),
        "verified_reads": [turn.verified_reads for turn in turns],
        "writes": [
            {
                "attempted": turn.write_attempted,
                "result": turn.write_result,
                "actions": turn.actions,
            }
            for turn in turns
        ],
        "replies": [turn.agent_response for turn in turns],
        "structured_operations": [_structured_ops(turn) for turn in turns],
        "active_tasks": [_active_task(turn) for turn in turns],
    }


def make_result(
    *,
    scenario_id: str,
    category: str,
    purpose: str,
    turns,
    before: dict[str, Any],
    after: dict[str, Any],
    verification: dict[str, Any],
    deterministic_ok: bool,
    expected: str,
    issue_severity: str = "P1",
    issue_title: str = "Deterministic contract mismatch",
    issue_detail: str = "",
) -> ScenarioResult:
    issues = []
    if not deterministic_ok:
        issues.append(
            {
                "severity": issue_severity,
                "title": issue_title,
                "detail": issue_detail,
            }
        )
    verification = {
        **verification,
        "db_delta": db_delta(before, after),
        "handoffs": [turn.handoff_state for turn in turns if turn.handoff_state],
    }
    return ScenarioResult(
        id=scenario_id,
        category=category,
        purpose=purpose,
        turns=list(turns),
        state_before=before,
        state_after=after,
        db_verification=verification,
        evaluation=default_evaluation(
            db_ok=deterministic_ok,
            grounding_ok=deterministic_ok,
            continuity_ok=deterministic_ok,
        ),
        issues=issues,
        token_usage={
            key: sum(int(turn.token_usage.get(key, 0)) for turn in turns)
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "cache_write_tokens",
                "uncached_input_tokens",
                "total_tokens",
                "calls",
                "metadata_missing_calls",
            )
        },
        review={
            "status": "PENDING_MANUAL_REVIEW",
            "expected": expected,
            "observed": _observed(turns),
            "reviewer_notes": "",
            "severity": None,
            "root_cause": None,
        },
    )


def laser_context(
    db: Session,
    workspace: Workspace,
    *,
    service_slug: str = "laser-hair-removal-underarm",
    device_key: str = "candela_gentle",
):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    service_model = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.slug == service_slug,
            Service.is_active.is_(True),
        )
    )
    if service_model is None:
        raise RuntimeError(f"EVAL_INFRA_ERROR: missing service {service_slug}")
    service = next(
        row
        for row in catalog.get("services", [])
        if isinstance(row, dict) and str(row.get("id")) == str(service_model.id)
    )
    doctors = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and row.get("id")
        and str(service["id"]) in {str(value) for value in (row.get("service_ids") or [])}
    ]
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    for doctor in doctors:
        for offset in range(1, 36):
            available = adapter.get_availability(
                AvailabilityRequest(
                    branch_id=branch_id,
                    service_id=str(service["id"]),
                    booking_date=today + timedelta(days=offset),
                    doctor_id=str(doctor["id"]),
                    laser_device_key=device_key,
                )
            )
            if available.slots:
                return catalog, service, doctor, available, available.slots[0]
    raise RuntimeError(
        f"EVAL_INFRA_ERROR: no {service_slug} slot for device={device_key}"
    )


def _alternate_laser_slot(
    db: Session,
    workspace: Workspace,
    *,
    service_id: str,
    doctor_id: str,
    branch_id: str,
    device_key: str,
    exclude_appointment_id: str | None = None,
):
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    for offset in range(1, 50):
        available = adapter.get_availability(
            AvailabilityRequest(
                branch_id=branch_id,
                service_id=service_id,
                booking_date=today + timedelta(days=offset),
                doctor_id=doctor_id,
                exclude_appointment_id=exclude_appointment_id,
                laser_device_key=device_key,
            )
        )
        if available.slots:
            return available, available.slots[0]
    raise RuntimeError("EVAL_INFRA_ERROR: no alternate laser slot")


def _seed_package(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    service: Service,
    *,
    remaining: int = 3,
) -> PatientPackage:
    package = PatientPackage(
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service.id,
        name=f"Batch2 {service.name} package",
        sessions_purchased=max(remaining, 3),
        opening_sessions_remaining=max(remaining, 3),
        sessions_total_known=True,
        sale_price_minor=max(service.price_minor * max(remaining, 3), 0),
        standalone_session_price_minor_at_purchase=service.price_minor,
        currency="EGP",
        purchased_at=datetime.now(UTC) - timedelta(days=60),
        status="active",
        source="staff",
        idempotency_key=f"batch2-package:{patient.id}:{service.id}",
    )
    db.add(package)
    db.flush()
    return package


def _seed_payment(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    *,
    amount_minor: int,
    days_ago: int,
) -> PaymentTransaction:
    row = PaymentTransaction(
        workspace_id=workspace.id,
        patient_id=patient.id,
        transaction_type="payment",
        amount_minor=max(amount_minor, 1),
        currency="EGP",
        payment_method="cash",
        source="staff",
        idempotency_key=f"batch2-payment:{patient.id}:{days_ago}:{amount_minor}",
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.add(row)
    db.flush()
    return row


def _compatible_doctor_id(
    catalog: dict[str, Any],
    service_id: UUID,
) -> UUID:
    row = next(
        doctor
        for doctor in catalog.get("doctors", [])
        if isinstance(doctor, dict)
        and doctor.get("id")
        and str(service_id) in {str(value) for value in (doctor.get("service_ids") or [])}
    )
    return UUID(str(row["id"]))


def _seed_historical_appointment(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    *,
    service: Service,
    doctor_id: UUID,
    branch_id: UUID,
    days_ago: int,
    status: str,
    billing_context: str = "standard",
    device_key: str | None = None,
    device_name: str | None = None,
) -> Appointment:
    start = datetime.now(UTC) - timedelta(days=days_ago)
    end = start + timedelta(minutes=max(int(service.duration_minutes or 60), 15))
    row = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch_id,
        doctor_id=doctor_id,
        service_id=service.id,
        status=status,
        source="staff",
        start_at=start,
        end_at=end,
        busy_start_at=start,
        busy_end_at=end,
        duration_minutes=max(int(service.duration_minutes or 60), 15),
        price_minor=service.price_minor,
        currency=service.currency,
        payment_status="paid" if status == "completed" else "unpaid",
        payment_method="cash" if status == "completed" else "unknown",
        amount_paid_minor=service.price_minor if status == "completed" else None,
        billing_context=billing_context,
        laser_device_key=device_key,
        laser_device_name=device_name,
        confirmed_at=start - timedelta(days=1),
        completed_at=end if status == "completed" else None,
        cancelled_at=start - timedelta(days=1) if status == "cancelled" else None,
    )
    db.add(row)
    db.flush()
    return row


def _history_pairs(
    *,
    stale_service: str = "PRP للبشرة",
    stale_doctor: str = "د. قديمة",
    stale_device: str = "Prime Lase",
) -> list[tuple[str, str]]:
    return [
        ("هاي", "أهلاً بيكي، إزاي أقدر أساعدك؟"),
        (f"كنت بسأل عن {stale_service}", f"تمام، كنا بنتكلم عن {stale_service}."),
        ("طب السعر كان كام؟", "اتراجع وقتها من بيانات العيادة."),
        ("ماشي احجزي الثلاثاء", "وقتها كملنا خطوات الحجز."),
        ("لا استنى غيريه الخميس", "وقتها اتراجع تغيير الموعد."),
        ("خلاص ألغي الميعاد", "وقتها اتراجع طلب الإلغاء."),
        ("طب الليزر عامل ايه", "ممكن نراجع خدمات الليزر المتاحة."),
        (f"كنت عايزة {stale_device}", f"تمام، ذكرتي جهاز {stale_device}."),
        ("أنا كنت اشتريت باقة Pulse صح؟", "كان عندنا كلام سابق عن باقة Pulse."),
        ("طب فاضل فيها كام؟", "كان لازم نراجعه من الرصيد المسجل."),
        ("وفاضلي جلسات في الباكدج؟", "كان لازم نراجع الباكدجات المسجلة."),
        (f"كنت بفضل {stale_doctor}", f"تمام، ذكرتي {stale_doctor}."),
        ("شكراً", "تحت أمرك."),
        ("رجعت أسأل عن PRP", "تمام."),
        ("لا خلينا ليزر", "حاضر."),
        ("سلام", "مع السلامة."),
    ]


def _seed_history_conversation(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    pairs: list[tuple[str, str]],
) -> Conversation:
    started = datetime.now(UTC) - timedelta(days=120)
    conversation = Conversation(
        workspace_id=workspace.id,
        patient_id=patient.id,
        channel="whatsapp",
        status="open",
        owner_type="ai",
        unread_count=0,
        ownership_changed_at=started,
        started_at=started,
        last_message_at=started,
    )
    db.add(conversation)
    db.flush()
    for index, (customer, assistant) in enumerate(pairs):
        customer_at = started + timedelta(days=index * 3, minutes=1)
        assistant_at = customer_at + timedelta(minutes=2)
        inbound = Message(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            sender_type="patient",
            direction="inbound",
            created_at=customer_at,
            message_type="text",
            content=customer,
            delivery_status="received",
            metadata_json={"batch2_seed": True},
        )
        db.add(inbound)
        db.flush()
        db.add(
            Message(
                workspace_id=workspace.id,
                conversation_id=conversation.id,
                sender_type="ai",
                direction="outbound",
                in_reply_to_message_id=inbound.id,
                created_at=assistant_at,
                message_type="text",
                content=assistant,
                delivery_status="sent",
                metadata_json={"batch2_seed": True},
            )
        )
        conversation.last_message_at = assistant_at
    db.flush()
    return conversation


def _conversation_message_count(
    db: Session,
    workspace: Workspace,
    conversation_id: UUID,
) -> int:
    return int(
        db.scalar(
            select(func.count(Message.id)).where(
                Message.workspace_id == workspace.id,
                Message.conversation_id == conversation_id,
            )
        )
        or 0
    )


def _seed_returning_history(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    *,
    stale_chat_service: str = "Hydrafacial",
) -> dict[str, Any]:
    catalog = build_clinic_catalog(db, workspace)
    branch_id = UUID(active_branch_id(catalog))
    prp = service_by_slug(db, workspace, "prp-skin")
    hydra = service_by_slug(db, workspace, "hydrafacial")
    prp_doctor = _compatible_doctor_id(catalog, prp.id)
    hydra_doctor = _compatible_doctor_id(catalog, hydra.id)
    completed = _seed_historical_appointment(
        db,
        workspace,
        patient,
        service=prp,
        doctor_id=prp_doctor,
        branch_id=branch_id,
        days_ago=35,
        status="completed",
    )
    cancelled = _seed_historical_appointment(
        db,
        workspace,
        patient,
        service=hydra,
        doctor_id=hydra_doctor,
        branch_id=branch_id,
        days_ago=20,
        status="cancelled",
    )
    package = _seed_package(db, workspace, patient, prp, remaining=2)
    _seed_payment(db, workspace, patient, amount_minor=prp.price_minor, days_ago=40)
    _seed_payment(
        db,
        workspace,
        patient,
        amount_minor=max(prp.price_minor // 2, 1),
        days_ago=10,
    )
    conversation = _seed_history_conversation(
        db,
        workspace,
        patient,
        _history_pairs(stale_service=stale_chat_service),
    )
    return {
        "conversation": conversation,
        "completed": completed,
        "cancelled": cancelled,
        "package": package,
    }


def case_01_compound_price_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "prp-skin")
    _, _, doctor, _, _, available = booking_context(
        db, workspace, service_slug="prp-skin"
    )
    slot = available.slots[0]
    date_text, time_text = local_slot(available, slot)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_01_compound_price_booking",
        [
            (
                f"{service.name} بكام؟ ولو مناسب عايزة أحجز يوم {date_text} "
                f"بعد الساعة {time_text} مع {doctor_name(doctor)}"
            ),
            f"تمام خدي الساعة {time_text}",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    price_ok = money(service.price_minor) in " ".join(
        (turn.agent_response or "").replace(",", "") for turn in turns
    )
    booking_ok = len(created) <= 1 and all(
        row["service_id"] == str(service.id) for row in created
    )
    return make_result(
        scenario_id="b2_01_compound_price_booking",
        category="compound",
        purpose="Price plus booking intent in one natural turn.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "canonical_price": service.price_minor,
            "price_grounded": price_ok,
            "created": created,
            "target_date": date_text,
            "target_time": time_text,
        },
        deterministic_ok=price_ok and booking_ok,
        expected=(
            "Verified PRP price plus correct booking progression without losing either intent."
        ),
        issue_title="Compound price + booking lost or corrupted a primary component",
        issue_detail="Price must be canonical and any write must remain on PRP skin.",
    )


def case_02_two_services_same_turn(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    price_service = service_by_slug(db, workspace, "prp-hair")
    _, laser_service, doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    date_text, time_text = local_slot(available, slot)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_02_two_services_same_turn",
        [
            (
                f"بصي {price_service.name} بكام، وكمان احجزيلي {laser_service['name']} "
                f"على كانديلا مع {doctor_name(doctor)} يوم {date_text} الساعة {time_text}"
            )
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    reply = (turns[-1].agent_response or "").replace(",", "")
    ok = (
        money(price_service.price_minor) in reply
        and len(created) == 1
        and created[0]["service_id"] == str(laser_service["id"])
        and created[0]["laser_device_key"] == "candela_gentle"
    )
    return make_result(
        scenario_id="b2_02_two_services_same_turn",
        category="compound",
        purpose="Keep pricing and booking scopes for two different services separate.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "price_service_id": str(price_service.id),
            "booking_service_id": str(laser_service["id"]),
            "created": created,
        },
        deterministic_ok=ok,
        expected=(
            "PRP hair price is answered while the appointment created is laser underarm on Candela."
        ),
        issue_title="Two-service compound turn conflated service scopes",
        issue_detail="The pricing service and booking service must remain independent.",
    )


def case_03_buy_pulse_and_book(db: Session, workspace: Workspace) -> ScenarioResult:
    offer = _active_offer_for_device(db, workspace, device_key="candela_gentle")
    patient = _active_patient(db, workspace)
    _, service, doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key=offer.device_key,
    )
    date_text, time_text = local_slot(available, slot)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_03_buy_pulse_and_book",
        [
            (
                f"عايزة أشتري باقة {offer.pulses_count} Pulse على {offer.device_name} "
                f"وكمان احجزيلي {service['name']} مع {doctor_name(doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            )
        ],
    )
    after = state_snapshot(db, workspace, patient)
    delta = db_delta(before, after)
    created = created_appointments(before, after)
    appointment = created[0] if len(created) == 1 else None
    expected_entitlement_delta = {offer.device_key: int(offer.pulses_count)}
    financial_safe = (
        delta["pulse_balance_delta"] == expected_entitlement_delta
        and not delta["pulse_usages"]["created"]
        and not delta["pulse_settlements"]["created"]
        and not delta["payments"]["created"]
    )
    ok = (
        len(delta["pulse_packs"]["created"]) == 1
        and appointment is not None
        and appointment["billing_context"] == "standard"
        and appointment["laser_device_key"] == offer.device_key
        and financial_safe
    )
    return make_result(
        scenario_id="b2_03_buy_pulse_and_book",
        category="pulse_compound",
        purpose="Purchase Pulse entitlement and book independently without settlement.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "created": created,
            "financial_safe": financial_safe,
            "expected_entitlement_delta": expected_entitlement_delta,
            "observed_pulse_balance_delta": delta["pulse_balance_delta"],
            "offer_id": str(offer.id),
        },
        deterministic_ok=ok,
        issue_severity="P0" if not financial_safe else "P1",
        expected=(
            "Pulse entitlement created; booking remains standard; no payment, usage, or settlement is invented."
        ),
        issue_title="Pulse purchase + booking violated the financial boundary",
        issue_detail="Any Pulse deduction, settlement, or payment is P0.",
    )


def case_04_explicit_use_pulses(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_with_pulse_balance(db, workspace)
    balance = next(
        row
        for row in list_patient_pulse_balances(
            db,
            workspace_id=workspace.id,
            patient_id=patient.id,
        )
        if row.pulses_remaining > 0
    )
    offer = _active_offer_for_device(db, workspace, device_key=balance.device_key)
    _, service, doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key=offer.device_key,
    )
    date_text, time_text = local_slot(available, slot)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_04_explicit_use_pulses",
        [
            (
                f"أنا عندي Pulses، احجزيلي {service['name']} على {offer.device_name} "
                f"مع {doctor_name(doctor)} يوم {date_text} الساعة {time_text} واحسبيها منهم"
            )
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    financial_safe = (
        not delta["pulse_balance_delta"]
        and not delta["pulse_usages"]["created"]
        and not delta["pulse_settlements"]["created"]
        and not delta["payments"]["created"]
    )
    ok = (
        len(created) == 1
        and created[0]["billing_context"] == "standard"
        and financial_safe
    )
    return make_result(
        scenario_id="b2_04_explicit_use_pulses",
        category="pulse_boundary",
        purpose="Customer asks Agent to bill from Pulses; Reception owns settlement.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "financial_safe": financial_safe},
        deterministic_ok=ok,
        issue_severity="P0" if not financial_safe else "P1",
        expected=(
            "Booking continues with standard billing, no Pulse mutation, and response explains Reception settlement ownership."
        ),
        issue_title="Agent crossed the Pulse settlement boundary",
        issue_detail="Pulse balance, usage, settlement, and payment must stay untouched.",
    )


def case_05_pulse_and_session_package(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_with_pulse_balance(db, workspace)
    service = service_by_slug(db, workspace, "prp-skin")
    package = _seed_package(db, workspace, patient, service, remaining=3)
    _, _, doctor, _, _, available = booking_context(
        db, workspace, service_slug="prp-skin"
    )
    date_text, time_text = local_slot(available, available.slots[0])
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_05_pulse_and_session_package",
        [
            (
                f"أنا عندي باكدج {service.name} وعندي Pulses كمان، "
                f"احجزيلي {service.name} من الباكدج مع {doctor_name(doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            )
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    pulse_safe = (
        not delta["pulse_balance_delta"]
        and not delta["pulse_usages"]["created"]
        and not delta["pulse_settlements"]["created"]
    )
    ok = (
        len(created) == 1
        and created[0]["patient_package_id"] == str(package.id)
        and created[0]["billing_context"] == "package_prepaid"
        and pulse_safe
    )
    return make_result(
        scenario_id="b2_05_pulse_and_session_package",
        category="package_pulse",
        purpose="Pulse balance must not override explicit session-package usage.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "package_id": str(package.id), "pulse_safe": pulse_safe},
        deterministic_ok=ok,
        issue_severity="P0" if not pulse_safe else "P1",
        expected="Session package backs appointment; Pulse state remains untouched.",
        issue_title="Pulse/session-package coexistence corrupted entitlement selection",
        issue_detail="Pulse existence must never override explicit package usage.",
    )


def case_06_pulse_overage(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _active_patient(db, workspace)
    rows = [
        row
        for row in list_pulse_billing_settings(db, workspace_id=workspace.id)
        if row.overage_price_minor is not None
    ]
    if not rows:
        raise RuntimeError("EVAL_INFRA_ERROR: no Pulse overage settings")
    row = next((item for item in rows if item.device_key == "candela_gentle"), rows[0])
    count = 1000
    expected_minor = count * int(row.overage_price_minor)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_06_pulse_overage",
        [f"لو احتجت {count} Pulse زيادة على {row.device_name} هيبقوا بكام؟"],
    )
    after = state_snapshot(db, workspace, patient)
    reply = (turns[-1].agent_response or "").replace(",", "")
    ok = (
        "pulse_billing_settings" in turns[-1].verified_reads
        and "pulse_pack_offers" not in turns[-1].verified_reads
        and money(expected_minor) in reply
        and before == after
    )
    return make_result(
        scenario_id="b2_06_pulse_overage",
        category="pulse_read",
        purpose="Counted overage uses verified per-device unit pricing.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "unit_price_minor": row.overage_price_minor,
            "count": count,
            "expected_total_minor": expected_minor,
        },
        deterministic_ok=ok,
        expected="Overage pricing only; deterministic total from verified unit price.",
        issue_title="Pulse overage was conflated or mispriced",
        issue_detail="Response should use only canonical overage pricing.",
    )


def case_07_pulse_pack_vs_overage(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _active_patient(db, workspace)
    offer = _active_offer_for_device(db, workspace, device_key="candela_gentle")
    billing = next(
        row
        for row in list_pulse_billing_settings(db, workspace_id=workspace.id)
        if row.device_key == offer.device_key
    )
    count = 500
    overage_total = count * int(billing.overage_price_minor)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_07_pulse_pack_vs_overage",
        [
            (
                f"وريني باقات {offer.device_name}، ولو عايزة {count} Pulse زيادة "
                "من غير باقة هيبقوا بكام؟"
            )
        ],
    )
    after = state_snapshot(db, workspace, patient)
    reply = (turns[-1].agent_response or "").replace(",", "")
    reads = set(turns[-1].verified_reads)
    ok = (
        {"pulse_pack_offers", "pulse_billing_settings"}.issubset(reads)
        and money(offer.price_minor) in reply
        and money(overage_total) in reply
        and before == after
    )
    return make_result(
        scenario_id="b2_07_pulse_pack_vs_overage",
        category="pulse_compound_read",
        purpose="Return pack offers and counted overage without conflation.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "offer_price_minor": offer.price_minor,
            "overage_total_minor": overage_total,
            "reads": sorted(reads),
        },
        deterministic_ok=ok,
        expected="Both verified pack offers and 500-Pulse overage total are answered independently.",
        issue_title="Pulse pack and overage compound read was conflated",
        issue_detail="Both canonical read sources must be represented without a write.",
    )


def case_08_pulse_financial_ledger_boundary(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_with_pulse_balance(db, workspace)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_08_pulse_financial_ledger_boundary",
        ["فاضل عليا كام فلوس في باقة الـPulse اللي اشتريتها؟"],
    )
    after = state_snapshot(db, workspace, patient)
    no_write = before == after and not turns[-1].write_attempted
    ok = no_write and (turns[-1].handoff_state is not None or bool(turns[-1].agent_response))
    return make_result(
        scenario_id="b2_08_pulse_financial_ledger_boundary",
        category="financial_boundary",
        purpose="Pulse payment-ledger question must not be hallucinated or mutated.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "no_write": no_write,
            "handoff_state": turns[-1].handoff_state,
            "verified_reads": turns[-1].verified_reads,
        },
        deterministic_ok=ok,
        expected="No unsupported ledger answer; Reception or handoff guidance.",
        issue_title="Pulse financial ledger boundary was not respected",
        issue_detail="No write is allowed; transcript requires manual truthfulness review.",
    )


def _seed_pulse_billed_future(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> Appointment:
    _, _, _, _, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    row = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=UUID(slot.branch_id),
        doctor_id=UUID(slot.doctor_id),
        service_id=UUID(slot.service_id),
        status="confirmed",
        source="staff",
        start_at=slot.start_at,
        end_at=slot.end_at,
        busy_start_at=slot.start_at,
        busy_end_at=slot.end_at,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        payment_status="paid",
        payment_method="cash",
        amount_paid_minor=0,
        billing_context="pulse_prepaid",
        laser_device_key="candela_gentle",
        laser_device_name=slot.laser_device_name or "Candela",
        confirmed_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def case_09_pulse_billed_reschedule(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    existing = _seed_pulse_billed_future(db, workspace, patient)
    before = state_snapshot(db, workspace, patient)
    available, target = _alternate_laser_slot(
        db,
        workspace,
        service_id=str(existing.service_id),
        doctor_id=str(existing.doctor_id),
        branch_id=str(existing.branch_id),
        device_key="candela_gentle",
        exclude_appointment_id=str(existing.id),
    )
    date_text, time_text = local_slot(available, target)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_09_pulse_billed_reschedule",
        ["ممكن أغير ميعادي؟", f"خليه يوم {date_text} الساعة {time_text}"],
    )
    after = state_snapshot(db, workspace, patient)
    original = next(row for row in after["appointments"] if row["id"] == str(existing.id))
    replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == str(existing.id)
    ]
    delta = db_delta(before, after)
    financial_safe = (
        not delta["payments"]["created"]
        and not delta["pulse_usages"]["created"]
        and not delta["pulse_settlements"]["created"]
        and not delta["pulse_balance_delta"]
    )
    ok = (
        original["status"] == "rescheduled"
        and len(replacements) == 1
        and replacements[0]["billing_context"] == "pulse_prepaid"
        and replacements[0]["laser_device_key"] == "candela_gentle"
        and financial_safe
    )
    return make_result(
        scenario_id="b2_09_pulse_billed_reschedule",
        category="appointment_lifecycle",
        purpose="Reschedule Pulse-billed appointment without new Agent billing decision.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "original": original,
            "replacement": replacements,
            "financial_safe": financial_safe,
            "target": target.start_at.isoformat(),
        },
        deterministic_ok=ok,
        issue_severity="P0" if not financial_safe else "P1",
        expected="Reschedule preserves pulse_prepaid state with no new Pulse/payment artifacts.",
        issue_title="Pulse-billed reschedule changed financial ownership",
        issue_detail="Agent must not create new financial effects during reschedule.",
    )


def case_10_pulse_billed_cancel(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    existing = _seed_pulse_billed_future(db, workspace, patient)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_10_pulse_billed_cancel",
        ["عايزة ألغي ميعادي اللي جاي", "أيوه الغيه"],
    )
    after = state_snapshot(db, workspace, patient)
    row = next(item for item in after["appointments"] if item["id"] == str(existing.id))
    delta = db_delta(before, after)
    financial_safe = (
        not delta["payments"]["created"]
        and not delta["pulse_usages"]["created"]
        and not delta["pulse_settlements"]["created"]
        and not delta["pulse_balance_delta"]
    )
    ok = row["status"] == "cancelled" and financial_safe
    return make_result(
        scenario_id="b2_10_pulse_billed_cancel",
        category="appointment_lifecycle",
        purpose="Cancel Pulse-billed appointment without Agent-side accounting.",
        turns=turns,
        before=before,
        after=after,
        verification={"appointment": row, "financial_safe": financial_safe},
        deterministic_ok=ok,
        issue_severity="P0" if not financial_safe else "P1",
        expected="Appointment cancels canonically; Agent performs no Pulse accounting.",
        issue_title="Pulse-billed cancellation crossed financial boundary",
        issue_detail="Cancellation must not create Pulse usage, settlement, or payment artifacts.",
    )


def case_11_change_service_mid_flow(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, prp, _, _, _, _ = booking_context(db, workspace, service_slug="prp-skin")
    _, hydra, hydra_doctor, _, _, hydra_available = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    date_text, time_text = local_slot(hydra_available, hydra_available.slots[0])
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_11_change_service_mid_flow",
        [
            f"عايزة أحجز {prp['name']}",
            f"ممكن يوم {date_text}؟",
            f"لا استنى خليها {hydra['name']} مع {doctor_name(hydra_doctor)} الساعة {time_text}",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) <= 1 and all(
        row["service_id"] == str(hydra["id"]) for row in created
    )
    return make_result(
        scenario_id="b2_11_change_service_mid_flow",
        category="correction",
        purpose="Changing service invalidates stale service-dependent state.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "expected_service_id": str(hydra["id"])},
        deterministic_ok=ok,
        expected="Hydrafacial replaces PRP; any eventual write uses only new service constraints.",
        issue_title="Old service leaked after mid-flow correction",
        issue_detail="Any created appointment must use Hydrafacial.",
    )


def case_12_change_doctor_mid_flow(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, first, second = context_with_two_doctors(db, workspace)
    doctor1, _, _ = first
    doctor2, _, available2 = second
    date_text, time_text = local_slot(available2, available2.slots[0])
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_12_change_doctor_mid_flow",
        [
            f"عايزة أحجز {service['name']} مع {doctor_name(doctor1)}",
            f"يوم {date_text}",
            f"لا خليني مع {doctor_name(doctor2)} الساعة {time_text}",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) <= 1 and all(
        row["doctor_id"] == str(doctor2["id"]) for row in created
    )
    return make_result(
        scenario_id="b2_12_change_doctor_mid_flow",
        category="correction",
        purpose="Latest explicit doctor replaces earlier doctor.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "expected_doctor_id": str(doctor2["id"])},
        deterministic_ok=ok,
        expected="Second doctor wins; no write may use stale first doctor.",
        issue_title="Stale doctor survived correction",
        issue_detail="Any created appointment must use the second doctor.",
    )


def case_13_change_device_mid_flow(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    date_text, time_text = local_slot(available, slot)
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_13_change_device_mid_flow",
        [
            f"عايزة أحجز {service['name']} على Prime Lase",
            f"يوم {date_text}",
            f"لا خليها Candela مع {doctor_name(doctor)} الساعة {time_text}",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) <= 1 and all(
        row["laser_device_key"] == "candela_gentle" for row in created
    )
    return make_result(
        scenario_id="b2_13_change_device_mid_flow",
        category="correction",
        purpose="Latest laser device replaces stale device-dependent state.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "expected_device": "candela_gentle"},
        deterministic_ok=ok,
        expected="Candela wins and availability/pricing scope is refreshed.",
        issue_title="Stale laser device survived correction",
        issue_detail="Any booking after correction must be Candela.",
    )


def case_14_conditional_fallback(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, doctor, _, _, available = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    slot = available.slots[0]
    first_date, first_time = local_slot(available, slot)
    second_date = (
        slot.start_at.astimezone(ZoneInfo(available.timezone)).date() + timedelta(days=1)
    ).isoformat()
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_14_conditional_fallback",
        [
            (
                f"شوفي {service['name']} مع {doctor_name(doctor)} يوم {first_date} "
                f"الساعة {first_time}، ولو مفيش شوفي {second_date} أقرب وقت"
            )
        ],
    )
    after = state_snapshot(db, workspace, patient)
    ops = _structured_ops(turns[-1])
    conditions = [
        op.get("continuation_condition") for op in ops if isinstance(op, dict)
    ]
    ok = (
        before == after
        and "availability" in turns[-1].verified_reads
        and "if_previous_no_availability" in conditions
    )
    return make_result(
        scenario_id="b2_14_conditional_fallback",
        category="availability",
        purpose="Fallback date is conditional and must not replace valid first choice.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "first_date": first_date,
            "first_time": first_time,
            "fallback_date": second_date,
            "structured_conditions": conditions,
        },
        deterministic_ok=ok,
        expected="Use first choice when available; inspect fallback only if first has no options.",
        issue_title="Conditional fallback semantics were lost",
        issue_detail="Fallback operation should declare if_previous_no_availability.",
    )


def _evening_context(db: Session, workspace: Workspace):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    services = [
        row
        for row in catalog.get("services", [])
        if isinstance(row, dict)
        and row.get("id")
        and not row.get("requires_laser_device")
    ]
    doctors = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict) and row.get("id")
    ]
    for service in services:
        for doctor in doctors:
            if str(service["id"]) not in {
                str(value) for value in (doctor.get("service_ids") or [])
            }:
                continue
            for offset in range(1, 30):
                available = adapter.get_availability(
                    AvailabilityRequest(
                        branch_id=branch_id,
                        service_id=str(service["id"]),
                        booking_date=today + timedelta(days=offset),
                        doctor_id=str(doctor["id"]),
                    )
                )
                for slot in available.slots:
                    local = slot.start_at.astimezone(ZoneInfo(available.timezone))
                    if local.hour > 12:
                        return service, doctor, available, slot
    raise RuntimeError("EVAL_INFRA_ERROR: no PM slot for ambiguity scenario")


def case_15_egyptian_time_ambiguity(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, doctor, available, slot = _evening_context(db, workspace)
    date_text, time_text = local_slot(available, slot)
    hour24 = int(time_text[:2])
    hour12 = hour24 - 12 if hour24 > 12 else hour24
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_15_egyptian_time_ambiguity",
        [
            f"عايزة أحجز {service['name']} مع {doctor_name(doctor)} يوم {date_text}",
            f"ينفع الساعة {hour12}؟",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) <= 1 and all(
        row["start_at"] == slot.start_at.isoformat() for row in created
    )
    return make_result(
        scenario_id="b2_15_egyptian_time_ambiguity",
        category="time_semantics",
        purpose="Resolve Egyptian 12-hour ambiguity deterministically.",
        turns=turns,
        before=before,
        after=after,
        verification={"spoken_hour": hour12, "expected_24h": time_text, "created": created},
        deterministic_ok=ok,
        expected=f"Ambiguous {hour12} resolves to clinic-valid {time_text}, not invented AM/PM.",
        issue_title="12-hour time ambiguity resolved incorrectly",
        issue_detail="Any created booking must match deterministic clinic-hours resolution.",
    )


def case_16_compare_doctors_then_select(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, first, second = context_with_two_doctors(db, workspace)
    doctor1, _, _ = first
    doctor2, _, _ = second
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_16_compare_doctors_then_select",
        [
            (
                f"عايزة أحجز {service['name']}، مين أقرب وقت متاح مع "
                f"{doctor_name(doctor1)} أو {doctor_name(doctor2)}؟"
            ),
            f"خلاص {doctor_name(doctor1)}",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    first_ops = _structured_ops(turns[0])
    set_compare = any(
        isinstance(op, dict)
        and isinstance(op.get("entities"), dict)
        and isinstance(op["entities"].get("doctor"), dict)
        and op["entities"]["doctor"].get("candidate_mode") == "set"
        for op in first_ops
    )
    wrong_writes = [
        row
        for row in created_appointments(before, after)
        if row["doctor_id"] != str(doctor1["id"])
    ]
    ok = set_compare and not wrong_writes
    return make_result(
        scenario_id="b2_16_compare_doctors_then_select",
        category="comparison",
        purpose="Doctor comparison is a set, then a short selection keeps continuity.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "first_turn_set_comparison": set_compare,
            "selected_doctor_id": str(doctor1["id"]),
            "wrong_writes": wrong_writes,
        },
        deterministic_ok=ok,
        expected="First turn compares set; second short turn selects without identity ambiguity.",
        issue_title="Doctor comparison or selection continuity failed",
        issue_detail="Comparison must use candidate_mode=set and no write may use unselected doctor.",
    )


def case_17_topic_switch_and_resume(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, laser, _, available, _ = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    date_text, _ = local_slot(available, available.slots[0])
    prp = service_by_slug(db, workspace, "prp-hair")
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_17_topic_switch_and_resume",
        [
            f"عايزة أحجز {laser['name']}",
            "على كانديلا",
            f"ممكن يوم {date_text}؟",
            f"بالمناسبة {prp.name} بكام؟",
            "تمام نكمل الحجز",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    price_grounded = money(prp.price_minor) in (turns[3].agent_response or "").replace(",", "")
    created = created_appointments(before, after)
    wrong_service_write = any(row["service_id"] != str(laser["id"]) for row in created)
    ok = price_grounded and not wrong_service_write and bool(turns[-1].agent_response)
    return make_result(
        scenario_id="b2_17_topic_switch_and_resume",
        category="topic_switching",
        purpose="Informational side intent must not destroy active booking task.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "side_price_grounded": price_grounded,
            "created": created,
            "resume_active_task": _active_task(turns[-1]),
        },
        deterministic_ok=ok,
        expected="PRP price is answered, then laser booking resumes without service leakage.",
        issue_title="Topic switch corrupted active booking continuity",
        issue_detail="Side pricing must not replace laser booking task.",
    )


def case_18_change_mind_completely(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, doctor, _, _, _ = booking_context(
        db, workspace, service_slug="prp-skin"
    )
    before = state_snapshot(db, workspace, patient)
    turns, conversation_id = run_messages(
        db,
        workspace,
        patient,
        "b2_18_change_mind_completely",
        [
            f"عايزة أحجز {service['name']} مع {doctor_name(doctor)}",
            "خلاص مش عايزة أحجز",
        ],
    )
    after = state_snapshot(db, workspace, patient)
    persisted = load_active_task(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
        patient_id=patient.id,
        run_id=None,
    )
    no_write = not created_appointments(before, after)
    cleared = persisted is None
    ok = no_write and cleared
    return make_result(
        scenario_id="b2_18_change_mind_completely",
        category="state_management",
        purpose="Abandon unfinished booking without business write.",
        turns=turns,
        before=before,
        after=after,
        verification={"no_write": no_write, "active_task_cleared": cleared},
        deterministic_ok=ok,
        expected="Active task closes and no appointment is created.",
        issue_title="Abandoned booking left stale state or performed write",
        issue_detail="No appointment write is allowed and active task must clear.",
    )


def case_19_duplicate_customer_message(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    date_text, time_text = local_slot(available, slot)
    message = (
        f"احجزيلي {service['name']} على كانديلا مع {doctor_name(doctor)} "
        f"يوم {date_text} الساعة {time_text}"
    )
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_19_duplicate_customer_message",
        [message, message],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) <= 1
    return make_result(
        scenario_id="b2_19_duplicate_customer_message",
        category="idempotency",
        purpose="Repeated customer text must not produce duplicate appointment writes.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "created": created,
            "scope_note": (
                "Distinct inbound messages; transport same-event idempotency is outside this harness."
            ),
        },
        deterministic_ok=ok,
        issue_severity="P0" if len(created) > 1 else "P1",
        expected="At most one appointment after repeated request.",
        issue_title="Repeated message produced duplicate business writes",
        issue_detail="Two appointments from repeated request is a dangerous duplicate write.",
    )


def case_20_ambiguous_correction(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, hair, doctor, _, _, available = booking_context(
        db, workspace, service_slug="prp-hair"
    )
    target_date, target_time = local_slot(available, available.slots[0])
    prior_date = (
        available.slots[0].start_at.astimezone(ZoneInfo(available.timezone)).date()
        - timedelta(days=1)
    ).isoformat()
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_20_ambiguous_correction",
        [
            (
                f"عايزة PRP يوم {prior_date} الساعة {target_time}، "
                f"لا استنى قصدي {target_date}، ومش البشرة الشعر مع {doctor_name(doctor)}"
            )
        ],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    serialized = json.dumps(_structured_ops(turns[-1]), ensure_ascii=False)
    final_date_present = target_date in serialized
    cancelled_date_absent = prior_date not in serialized or prior_date == target_date
    ok = (
        len(created) <= 1
        and all(row["service_id"] == str(hair["id"]) for row in created)
        and final_date_present
        and cancelled_date_absent
    )
    return make_result(
        scenario_id="b2_20_ambiguous_correction",
        category="correction",
        purpose="Resolve same-turn corrections jointly; superseded values disappear.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "final_service_id": str(hair["id"]),
            "final_date": target_date,
            "cancelled_date": prior_date,
            "created": created,
        },
        deterministic_ok=ok,
        expected="Final meaning is PRP hair on corrected date/time only.",
        issue_title="Same-turn correction retained superseded values",
        issue_detail="Latest corrected service and date must win jointly.",
    )


def case_21_long_returning_customer(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_with_pulse_balance(db, workspace)
    seeded = _seed_returning_history(db, workspace, patient)
    conversation = seeded["conversation"]
    history_before = _conversation_message_count(db, workspace, conversation.id)
    _, laser, laser_doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    target_date, target_time = local_slot(available, slot)
    prp = service_by_slug(db, workspace, "prp-skin")
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_21_long_returning_customer",
        [
            "هاي، أنا كنت جيت عندكم قبل كده، آخر حاجة عملتها كانت إيه؟",
            "طب مين الدكاترة اللي بيعملوا نفس الخدمة؟",
            "وبالمرة فاضلي كام Pulse؟",
            f"سيبي ده دلوقتي، {prp.name} بكام؟",
            f"لا رجعت لليزر، عايزة {laser['name']}",
            "على كانديلا",
            f"ممكن يوم {target_date}؟",
            f"طب مع {doctor_name(laser_doctor)}؟",
            f"ماشي الساعة {target_time} احجزي",
        ],
        conversation_id=conversation.id,
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    old_leak = any(
        row["service_id"] != str(laser["id"])
        or row["laser_device_key"] != "candela_gentle"
        for row in created
    )
    ok = (
        history_before > settings.agent_history_messages
        and len(created) <= 1
        and not old_leak
        and any("customer_history" in turn.verified_reads for turn in turns)
        and any("pulse_balance" in turn.verified_reads for turn in turns)
    )
    return make_result(
        scenario_id="b2_21_long_returning_customer",
        category="long_history",
        purpose="Returning patient with rich historical business state and long prior chat.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "historical_messages_before_current": history_before,
            "history_loader_limit": settings.agent_history_messages,
            "native_interpreter_limit": NATIVE_HISTORY_LIMIT,
            "historical_completed_id": str(seeded["completed"].id),
            "historical_cancelled_id": str(seeded["cancelled"].id),
            "historical_package_id": str(seeded["package"].id),
            "created": created,
            "old_context_leakage": old_leak,
            "context_metrics": [turn.context_metrics for turn in turns],
        },
        deterministic_ok=ok,
        expected=(
            "Verified history and Pulse facts survive long history and topic switches; final explicit Candela booking wins."
        ),
        issue_title="Long returning-customer history degraded canonical continuity",
        issue_detail="DB facts and current explicit constraints must remain correct beyond history limits.",
    )


def case_22_long_db_wins_over_stale_chat(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    seeded = _seed_returning_history(
        db,
        workspace,
        patient,
        stale_chat_service="Hydrafacial",
    )
    conversation = seeded["conversation"]
    history_before = _conversation_message_count(db, workspace, conversation.id)
    prp = service_by_slug(db, workspace, "prp-skin")
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_22_long_db_wins_over_stale_chat",
        [
            "أنا عملت إيه آخر مرة؟",
            "متأكدة؟ أنا فاكرة إن الشات القديم كان بيقول Hydrafacial",
            "تمام، عايزة أحجز نفس الحاجة اللي عملتها فعلًا آخر مرة",
        ],
        conversation_id=conversation.id,
    )
    after = state_snapshot(db, workspace, patient)
    transcript = " ".join((turn.agent_response or "") for turn in turns)
    db_fact_grounded = prp.name.casefold() in transcript.casefold()
    created = created_appointments(before, after)
    no_stale_wrong_write = all(row["service_id"] == str(prp.id) for row in created)
    ok = (
        history_before > settings.agent_history_messages
        and db_fact_grounded
        and no_stale_wrong_write
        and any("customer_history" in turn.verified_reads for turn in turns)
    )
    return make_result(
        scenario_id="b2_22_long_db_wins_over_stale_chat",
        category="long_history",
        purpose="Canonical DB history must beat intentionally stale conversational memory.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "canonical_last_service": prp.name,
            "stale_chat_service": "Hydrafacial",
            "historical_messages_before_current": history_before,
            "db_fact_grounded": db_fact_grounded,
            "created": created,
            "context_metrics": [turn.context_metrics for turn in turns],
        },
        deterministic_ok=ok,
        expected=(
            "Agent states DB-backed last service, not stale chat, and follow-on booking remains canonical."
        ),
        issue_title="Stale conversational fact overrode canonical DB history",
        issue_detail="customer_history DB read must win over old assistant prose.",
    )


def _control_target(db: Session, workspace: Workspace):
    _, service, doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    date_text, time_text = local_slot(available, slot)
    return service, doctor, slot, date_text, time_text


def case_23_long_old_context_no_leak(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, doctor, slot, date_text, time_text = _control_target(db, workspace)
    conversation = _seed_history_conversation(
        db,
        workspace,
        patient,
        _history_pairs(
            stale_service="PRP للبشرة",
            stale_doctor="د. مها القديمة",
            stale_device="Prime Lase",
        ),
    )
    history_before = _conversation_message_count(db, workspace, conversation.id)
    before = state_snapshot(db, workspace, patient)
    message = (
        f"المرة دي عايزة {service['name']} على كانديلا مع {doctor_name(doctor)} "
        f"يوم {date_text} الساعة {time_text}، احجزي"
    )
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_23_long_old_context_no_leak",
        [message],
        conversation_id=conversation.id,
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = (
        history_before > settings.agent_history_messages
        and len(created) == 1
        and created[0]["service_id"] == str(service["id"])
        and created[0]["doctor_id"] == str(doctor["id"])
        and created[0]["laser_device_key"] == "candela_gentle"
        and created[0]["start_at"] == slot.start_at.isoformat()
    )
    return make_result(
        scenario_id="b2_23_long_old_context_no_leak",
        category="long_history_control",
        purpose="Explicit current request overrides stale old context beyond history limits.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "historical_messages_before_current": history_before,
            "created": created,
            "control_target": {
                "service_id": str(service["id"]),
                "doctor_id": str(doctor["id"]),
                "date": date_text,
                "time": time_text,
                "start_at": slot.start_at.isoformat(),
            },
            "context_metrics": [turn.context_metrics for turn in turns],
        },
        deterministic_ok=ok,
        expected=(
            "Current underarm, Candela, doctor, date, and time win completely; no old context leakage."
        ),
        issue_title="Old context leaked into explicit current booking",
        issue_detail="Every explicit current dimension must override stale chat history.",
    )


def case_24_short_control(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, doctor, slot, date_text, time_text = _control_target(db, workspace)
    message = (
        f"عايزة {service['name']} على كانديلا مع {doctor_name(doctor)} "
        f"يوم {date_text} الساعة {time_text}، احجزي"
    )
    before = state_snapshot(db, workspace, patient)
    turns, _ = run_messages(
        db,
        workspace,
        patient,
        "b2_24_short_control",
        [message],
    )
    after = state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = (
        len(created) == 1
        and created[0]["service_id"] == str(service["id"])
        and created[0]["doctor_id"] == str(doctor["id"])
        and created[0]["laser_device_key"] == "candela_gentle"
        and created[0]["start_at"] == slot.start_at.isoformat()
    )
    return make_result(
        scenario_id="b2_24_short_control",
        category="short_control",
        purpose="Controlled short equivalent of long old-context booking.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "created": created,
            "control_target": {
                "service_id": str(service["id"]),
                "doctor_id": str(doctor["id"]),
                "date": date_text,
                "time": time_text,
                "start_at": slot.start_at.isoformat(),
            },
            "context_metrics": [turn.context_metrics for turn in turns],
        },
        deterministic_ok=ok,
        expected="Same final booking correctness as long-history control with minimal history overhead.",
        issue_title="Short control booking failed",
        issue_detail="Short baseline must create exact explicit Candela booking.",
    )


CASES: list[ScenarioFn] = [
    case_01_compound_price_booking,
    case_02_two_services_same_turn,
    case_03_buy_pulse_and_book,
    case_04_explicit_use_pulses,
    case_05_pulse_and_session_package,
    case_06_pulse_overage,
    case_07_pulse_pack_vs_overage,
    case_08_pulse_financial_ledger_boundary,
    case_09_pulse_billed_reschedule,
    case_10_pulse_billed_cancel,
    case_11_change_service_mid_flow,
    case_12_change_doctor_mid_flow,
    case_13_change_device_mid_flow,
    case_14_conditional_fallback,
    case_15_egyptian_time_ambiguity,
    case_16_compare_doctors_then_select,
    case_17_topic_switch_and_resume,
    case_18_change_mind_completely,
    case_19_duplicate_customer_message,
    case_20_ambiguous_correction,
    case_21_long_returning_customer,
    case_22_long_db_wins_over_stale_chat,
    case_23_long_old_context_no_leak,
    case_24_short_control,
]



def run_case(engine, workspace_slug: str, case_fn: ScenarioFn) -> ScenarioResult:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        acquire_eval_advisory_lock(db, namespace="tia-agent-eval-batch-02")
        workspace = db.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug)
        )
        if workspace is None:
            raise RuntimeError("Workspace not found")
        assert_demo_only(workspace)
        _ensure_batch2_catalog_fixtures(db, workspace)
        return case_fn(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult(
            id=case_fn.__name__.removeprefix("case_"),
            category="infrastructure",
            purpose="Scenario execution failed before review.",
            turns=[],
            state_before={},
            state_after={},
            db_verification={},
            evaluation=default_evaluation(db_ok=False, grounding_ok=False),
            issues=[],
            token_usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "uncached_input_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
                "metadata_missing_calls": 0,
            },
            execution_error=f"{type(exc).__name__}: {exc}",
            review={
                "status": "INFRASTRUCTURE_FAILURE",
                "expected": "",
                "observed": {},
                "reviewer_notes": f"{type(exc).__name__}: {exc}",
                "severity": None,
                "root_cause": "Infrastructure/provider noise or test data problem",
            },
        )
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def _call_metrics(row: ScenarioResult) -> dict[str, Any]:
    calls = [call for turn in row.turns for call in turn.llm_calls]
    interpreter = [call for call in calls if call.get("operation") == "v2-turn-interpreter"]
    responder = [call for call in calls if call.get("operation") == "v2-responder"]

    def sums(selected):
        return {
            "input_tokens": sum(int(call.get("input_tokens_actual") or 0) for call in selected),
            "cached_read_tokens": sum(int(call.get("cached_tokens_actual") or 0) for call in selected),
            "cache_write_tokens": sum(int(call.get("cache_write_tokens_actual") or 0) for call in selected),
            "uncached_tokens": sum(int(call.get("uncached_input_tokens_actual") or 0) for call in selected),
            "output_tokens": sum(int(call.get("output_tokens_actual") or 0) for call in selected),
            "latency_ms": sum(int(call.get("latency_ms") or 0) for call in selected),
            "retries": sum(int(call.get("retry_count") or 0) for call in selected),
            "fallback_calls": sum(bool(call.get("fallback_used")) for call in selected),
            "calls": len(selected),
        }

    return {
        "all": sums(calls),
        "interpreter": sums(interpreter),
        "responder": sums(responder),
        "turn_latency_ms": sum(turn.latency_ms for turn in row.turns),
    }


def _apply_cost(
    row: ScenarioResult,
    *,
    input_price: float,
    cached_price: float,
    output_price: float,
    cache_write_multiplier: float,
) -> None:
    usage = row.token_usage
    normal = input_price / 1_000_000
    cached = cached_price / 1_000_000
    output = output_price / 1_000_000
    actual_input = (
        int(usage.get("uncached_input_tokens", 0)) * normal
        + int(usage.get("cached_tokens", 0)) * cached
        + int(usage.get("cache_write_tokens", 0)) * normal * cache_write_multiplier
    )
    output_cost = int(usage.get("output_tokens", 0)) * output
    without_cache_input = int(usage.get("input_tokens", 0)) * normal
    row.cost = {
        "actual_input_usd": round(actual_input, 8),
        "output_usd": round(output_cost, 8),
        "actual_total_usd": round(actual_input + output_cost, 8),
        "without_explicit_cache_usd": round(without_cache_input + output_cost, 8),
        "cache_saving_usd": round(max(0.0, without_cache_input - actual_input), 8),
        "cache_saving_percent": round(
            ((without_cache_input - actual_input) / without_cache_input * 100.0)
            if without_cache_input > 0
            else 0.0,
            2,
        ),
    }


def _short_long_comparison(results: list[ScenarioResult]) -> dict[str, Any]:
    by_id = {row.id: row for row in results}
    short = by_id.get("b2_24_short_control")
    long = by_id.get("b2_23_long_old_context_no_leak")
    if short is None or long is None:
        return {}
    short_metrics = _call_metrics(short)
    long_metrics = _call_metrics(long)
    return {
        "short_id": short.id,
        "long_id": long.id,
        "short_deterministic_ok": not short.issues and short.execution_error is None,
        "long_deterministic_ok": not long.issues and long.execution_error is None,
        "short_turns": len(short.turns),
        "long_turns": len(long.turns),
        "short_llm_calls": short_metrics["all"]["calls"],
        "long_llm_calls": long_metrics["all"]["calls"],
        "short_input_tokens": short.token_usage["input_tokens"],
        "long_input_tokens": long.token_usage["input_tokens"],
        "short_cached_tokens": short.token_usage["cached_tokens"],
        "long_cached_tokens": long.token_usage["cached_tokens"],
        "short_uncached_tokens": short.token_usage["uncached_input_tokens"],
        "long_uncached_tokens": long.token_usage["uncached_input_tokens"],
        "short_latency_ms": short_metrics["turn_latency_ms"],
        "long_latency_ms": long_metrics["turn_latency_ms"],
        "input_growth_percent": round(
            (
                (long.token_usage["input_tokens"] - short.token_usage["input_tokens"])
                / short.token_usage["input_tokens"]
                * 100.0
            )
            if short.token_usage["input_tokens"]
            else 0.0,
            2,
        ),
        "latency_growth_percent": round(
            (
                (long_metrics["turn_latency_ms"] - short_metrics["turn_latency_ms"])
                / short_metrics["turn_latency_ms"]
                * 100.0
            )
            if short_metrics["turn_latency_ms"]
            else 0.0,
            2,
        ),
    }


def summarize(results: list[ScenarioResult]) -> dict[str, Any]:
    tokens = batch_token_summary(results)
    return {
        "scenarios_run": len(results),
        "pending_manual_review": sum(
            row.review.get("status") == "PENDING_MANUAL_REVIEW" for row in results
        ),
        "infrastructure_failures": sum(row.execution_error is not None for row in results),
        "deterministic_P0": sum(
            issue.get("severity") == "P0"
            for row in results
            for issue in row.issues
        ),
        "deterministic_P1": sum(
            issue.get("severity") == "P1"
            for row in results
            for issue in row.issues
        ),
        "tokens": tokens,
        "total_turns": sum(len(row.turns) for row in results),
        "total_llm_calls": sum(
            len(turn.llm_calls) for row in results for turn in row.turns
        ),
        "short_vs_long": _short_long_comparison(results),
    }


def write_reports(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Linka Agent Evaluation — Batch 02 Raw Baseline",
        "",
        f"- Batch runtime base SHA: {payload['run_metadata']['batch2_base_sha']}",
        f"- Harness SHA: {payload['run_metadata']['git_sha']}",
        f"- Scenario version: {payload['run_metadata']['scenario_version']}",
        f"- Model: {payload['run_metadata']['model']}",
        f"- Reasoning: {payload['run_metadata']['reasoning_effort']}",
        f"- Scenarios executed: {len(payload['scenario_results'])}",
        "",
        "Raw results are intentionally unreviewed. Deterministic findings are guards, not final PASS/FAIL.",
        "",
    ]
    for row in payload["scenario_results"]:
        lines.extend(
            [
                f"## {row['id']}",
                "",
                f"Category: {row['category']}",
                f"Purpose: {row['purpose']}",
                f"Review status: {row['review']['status']}",
                f"Expected: {row['review']['expected']}",
                "",
            ]
        )
        for turn in row["turns"]:
            lines.append(f"Customer {turn['turn_number']}: {turn['user_message']}")
            lines.append(f"Linka: {turn['agent_response']}")
            lines.append(
                "Usage: "
                f"in={turn['token_usage']['input_tokens']} "
                f"read={turn['token_usage']['cached_tokens']} "
                f"write={turn['token_usage']['cache_write_tokens']} "
                f"uncached={turn['token_usage']['uncached_input_tokens']} "
                f"out={turn['token_usage']['output_tokens']} "
                f"latency={turn['latency_ms']}ms"
            )
            lines.append("")
        lines.append("DB verification:")
        lines.append(json.dumps(row["db_verification"], ensure_ascii=False, indent=2))
        if row["issues"]:
            lines.append("Deterministic findings:")
            for issue in row["issues"]:
                lines.append(
                    f"- {issue['severity']}: {issue['title']} — {issue['detail']}"
                )
        else:
            lines.append("Deterministic findings: none; manual review still required.")
        lines.append("")

    lines.extend(
        [
            "## Batch summary",
            "",
            json.dumps(payload["batch_summary"], ensure_ascii=False, indent=2),
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ns = parse_args()
    require_explicit_demo_eval()
    if not all(
        value > 0
        for value in (
            ns.input_price_per_million,
            ns.cached_input_price_per_million,
            ns.output_price_per_million,
        )
    ):
        raise RuntimeError("Current provider pricing must be supplied explicitly.")

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        workspace = db.scalar(
            select(Workspace).where(Workspace.slug == ns.workspace_slug)
        )
        if workspace is None:
            raise RuntimeError("Demo workspace not found.")
        assert_demo_only(workspace)
        demo_seed = {
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "history_loader_limit": settings.agent_history_messages,
            "native_interpreter_history_limit": NATIVE_HISTORY_LIMIT,
            "fixture_version": BATCH2_FIXTURE_VERSION,
        }

    results: list[ScenarioResult] = []
    stopped_for_p0 = False
    for case_fn in CASES:
        row = run_case(engine, ns.workspace_slug, case_fn)
        _apply_cost(
            row,
            input_price=ns.input_price_per_million,
            cached_price=ns.cached_input_price_per_million,
            output_price=ns.output_price_per_million,
            cache_write_multiplier=ns.cache_write_multiplier,
        )
        results.append(row)
        if any(issue.get("severity") == "P0" for issue in row.issues):
            stopped_for_p0 = True
            break

    summary = summarize(results)
    total_actual_cost = sum(float(row.cost.get("actual_total_usd") or 0) for row in results)
    total_without_cache = sum(
        float(row.cost.get("without_explicit_cache_usd") or 0) for row in results
    )
    summary["cost"] = {
        "actual_usd": round(total_actual_cost, 8),
        "without_explicit_cache_usd": round(total_without_cache, 8),
        "saving_usd": round(max(0.0, total_without_cache - total_actual_cost), 8),
        "saving_percent": round(
            (
                (total_without_cache - total_actual_cost)
                / total_without_cache
                * 100.0
            )
            if total_without_cache > 0
            else 0.0,
            2,
        ),
    }
    summary["stopped_for_deterministic_p0"] = stopped_for_p0

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(ns.output_dir)
    payload = {
        "run_metadata": {
            "batch": BATCH_NUMBER,
            "scenario_version": SCENARIO_VERSION,
            "git_sha": ns.git_sha,
            "batch2_base_sha": ns.batch2_base_sha,
            "workspace": ns.workspace_slug,
            "model": settings.openai_model,
            "fallback_model": settings.openai_fallback_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "fallback_reasoning_effort": settings.openai_fallback_reasoning_effort,
            "generated_at": datetime.now(UTC).isoformat(),
            "pricing": {
                "input_per_million": ns.input_price_per_million,
                "cached_input_per_million": ns.cached_input_price_per_million,
                "output_per_million": ns.output_price_per_million,
                "cache_write_multiplier": ns.cache_write_multiplier,
                "source": ns.pricing_source,
            },
            "demo_seed": demo_seed,
        },
        "scenario_results": [jsonable(row) for row in results],
        "batch_summary": summary,
    }
    json_path = output_dir / f"batch_02_raw_{timestamp}.json"
    md_path = output_dir / f"batch_02_raw_{timestamp}.md"
    write_reports(payload, json_path, md_path)

    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    ).decode("ascii")
    print("EVAL_REPORT_B64_BEGIN", flush=True)
    for offset in range(0, len(encoded), 3000):
        print(f"EVAL_REPORT_B64={encoded[offset:offset + 3000]}", flush=True)
    print("EVAL_REPORT_B64_END", flush=True)
    print(f"JSON_RESULT={json_path}")
    print(f"MD_RESULT={md_path}")
    print(f"SCENARIOS_RUN={len(results)}")
    print(f"TOTAL_TURNS={summary['total_turns']}")
    print(f"TOTAL_LLM_CALLS={summary['total_llm_calls']}")
    print(f"TOTAL_TOKENS={summary['tokens']['total_tokens']}")
    print(f"ACTUAL_COST_USD={summary['cost']['actual_usd']}")
    print(f"WITHOUT_CACHE_USD={summary['cost']['without_explicit_cache_usd']}")
    print(f"CACHE_SAVING_PERCENT={summary['cost']['saving_percent']}")
    return 2 if stopped_for_p0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
