from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.automation_rules import DEFAULT_AUTOMATION_RULES
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.appointment_status_history import AppointmentStatusHistory
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.automation_worker import AutomationWorker
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.channel_delivery_event import ChannelDeliveryEvent
from app.models.channel_identity import ChannelIdentity
from app.models.channel_inbound_event import ChannelInboundEvent
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.handoff_event import HandoffEvent
from app.models.handoff_request import HandoffRequest
from app.models.lead import Lead
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.patient_note import PatientNote
from app.models.patient_tag import PatientTag, PatientTagAssignment
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember

from staging_scenarios import (
    APPOINTMENT_KEYS,
    AUTOMATION_JOB_KEYS,
    CONVERSATION_KEYS,
    HANDOFF_KEYS,
    MOCK_AUTOMATION_TOKEN,
    MOCK_CHANNEL_TOKEN,
    MOCK_PAUSED_CHANNEL_TOKEN,
    PATIENT_KEYS,
    REGRESSION_WORKSPACE_ID,
    REGRESSION_WORKSPACE_SLUG,
    SEED_MARKER,
    SEED_VERSION,
    sid,
)


def require_staging() -> None:
    if settings.is_production:
        raise RuntimeError("Refusing to seed test data because ENVIRONMENT=production.")
    if settings.environment.lower() not in {"staging", "development", "dev", "local", "test"}:
        raise RuntimeError(
            f"Refusing to seed an unknown environment: {settings.environment!r}. "
            "Use staging/development/local/test."
        )


def local_at(days: int, hour: int, minute: int = 0) -> datetime:
    tz = ZoneInfo("Africa/Cairo")
    today = datetime.now(tz).date() + timedelta(days=days)
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)


def stable_created(now: datetime, minutes_ago: int = 0) -> datetime:
    return now - timedelta(minutes=minutes_ago)


def upsert_by_id(db: Session, model, row_id: UUID, **values):
    row = db.get(model, row_id)
    if row is None:
        row = model(id=row_id, **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.flush()
    return row


def ensure_clinic(
    db: Session,
    workspace: Workspace,
) -> tuple[Branch, Branch, Doctor, Doctor, Service, Service, Service]:
    branch_main = upsert_by_id(
        db,
        Branch,
        sid(workspace.id, "branch:regression-main"),
        workspace_id=workspace.id,
        name="Regression Cairo Branch",
        code="regression-main",
        phone="+200000100001",
        email="regression-main@tia.example",
        address_line1="Staging regression address",
        city="Cairo",
        country_code="EG",
        timezone="Africa/Cairo",
        is_active=True,
    )
    branch_secondary = upsert_by_id(
        db,
        Branch,
        sid(workspace.id, "branch:regression-secondary"),
        workspace_id=workspace.id,
        name="Regression New Cairo Branch",
        code="regression-new-cairo",
        phone="+200000100002",
        email="regression-new-cairo@tia.example",
        address_line1="Staging regression New Cairo",
        city="New Cairo",
        country_code="EG",
        timezone="Africa/Cairo",
        is_active=True,
    )

    staff_main = upsert_by_id(
        db,
        Staff,
        sid(workspace.id, "staff:regression-doctor-main"),
        workspace_id=workspace.id,
        user_id=None,
        first_name="ريجريشن",
        last_name="الأول",
        email="regression-doctor-1@tia.example",
        phone="+200000110001",
        job_title="Regression Doctor",
        is_active=True,
    )
    staff_second = upsert_by_id(
        db,
        Staff,
        sid(workspace.id, "staff:regression-doctor-second"),
        workspace_id=workspace.id,
        user_id=None,
        first_name="سارة",
        last_name="تجريبية",
        email="regression-doctor-2@tia.example",
        phone="+200000110002",
        job_title="Regression Doctor",
        is_active=True,
    )
    doctor_main = upsert_by_id(
        db,
        Doctor,
        sid(workspace.id, "doctor:regression-main"),
        workspace_id=workspace.id,
        staff_id=staff_main.id,
        specialization="Aesthetic Medicine — Regression",
        license_number="REG-001",
        bio="Staging-only regression doctor.",
        booking_enabled=True,
        is_active=True,
    )
    doctor_second = upsert_by_id(
        db,
        Doctor,
        sid(workspace.id, "doctor:regression-second"),
        workspace_id=workspace.id,
        staff_id=staff_second.id,
        specialization="Dermatology — Regression",
        license_number="REG-002",
        bio="Staging-only regression doctor.",
        booking_enabled=True,
        is_active=True,
    )

    laser = upsert_by_id(
        db,
        Service,
        sid(workspace.id, "service:regression-laser"),
        workspace_id=workspace.id,
        name="ليزر ريجريشن",
        slug="regression-laser",
        category="Laser",
        description="خدمة staging لاختبارات Linka.",
        duration_minutes=60,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=150000,
        currency="EGP",
        requires_medical_review=False,
        is_active=True,
    )
    botox = upsert_by_id(
        db,
        Service,
        sid(workspace.id, "service:regression-botox"),
        workspace_id=workspace.id,
        name="بوتوكس ريجريشن",
        slug="regression-botox",
        category="Injectables",
        description="خدمة staging تحتاج مراجعة طبية.",
        duration_minutes=45,
        buffer_before_minutes=15,
        buffer_after_minutes=15,
        price_minor=250000,
        currency="EGP",
        requires_medical_review=True,
        is_active=True,
    )
    facial = upsert_by_id(
        db,
        Service,
        sid(workspace.id, "service:regression-facial"),
        workspace_id=workspace.id,
        name="تنظيف بشرة ريجريشن",
        slug="regression-facial",
        category="Facial",
        description="خدمة staging قصيرة.",
        duration_minutes=30,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        price_minor=90000,
        currency="EGP",
        requires_medical_review=False,
        is_active=True,
    )

    for doctor, branch, primary in (
        (doctor_main, branch_main, True),
        (doctor_main, branch_secondary, False),
        (doctor_second, branch_secondary, True),
        (doctor_second, branch_main, False),
    ):
        upsert_by_id(
            db,
            DoctorBranch,
            sid(workspace.id, f"doctor-branch:{doctor.id}:{branch.id}"),
            workspace_id=workspace.id,
            doctor_id=doctor.id,
            branch_id=branch.id,
            is_primary=primary,
            is_active=True,
        )

    for doctor, service, custom_duration, custom_price in (
        (doctor_main, laser, None, None),
        (doctor_main, facial, None, None),
        (doctor_second, laser, 60, 160000),
        (doctor_second, botox, None, None),
    ):
        upsert_by_id(
            db,
            DoctorService,
            sid(workspace.id, f"doctor-service:{doctor.id}:{service.id}"),
            workspace_id=workspace.id,
            doctor_id=doctor.id,
            service_id=service.id,
            custom_duration_minutes=custom_duration,
            custom_price_minor=custom_price,
            is_active=True,
        )

    for weekday in range(7):
        for branch in (branch_main, branch_secondary):
            upsert_by_id(
                db,
                BranchWorkingHour,
                sid(workspace.id, f"branch-hours:{branch.id}:{weekday}"),
                workspace_id=workspace.id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=time(10, 0),
                end_time=time(22, 0),
            )
        for doctor, branch in (
            (doctor_main, branch_main),
            (doctor_main, branch_secondary),
            (doctor_second, branch_main),
            (doctor_second, branch_secondary),
        ):
            upsert_by_id(
                db,
                DoctorWorkingHour,
                sid(workspace.id, f"doctor-hours:{doctor.id}:{branch.id}:{weekday}"),
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=time(10, 0),
                end_time=time(22, 0),
            )

    booking = db.scalar(
        select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
    )
    if booking is None:
        booking = BookingSettings(workspace_id=workspace.id)
        db.add(booking)
    booking.slot_interval_minutes = 15
    booking.minimum_notice_minutes = 30
    booking.booking_horizon_days = 90
    booking.cancellation_notice_minutes = 1440
    booking.allow_same_day_booking = True
    booking.require_confirmation = True
    booking.default_currency = "EGP"
    db.flush()

    return branch_main, branch_secondary, doctor_main, doctor_second, laser, botox, facial


def cleanup_seed_owned(db: Session, workspace_id: UUID) -> None:
    patient_ids = [sid(workspace_id, f"patient:{key}") for key in PATIENT_KEYS]
    connection_ids = [
        sid(workspace_id, "channel:mock-whatsapp"),
        sid(workspace_id, "channel:paused-instagram"),
    ]

    # Delete by seed patient, not only by deterministic row ids. Regression runs can
    # create replacement appointments, AI conversations, messages and handoffs with
    # random ids. This guarantees a truly clean reset before the next run.
    db.execute(
        delete(AutomationJob).where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.patient_id.in_(patient_ids),
        )
    )
    # Conversations can be created by later regression suites with random
    # patient IDs while still using these deterministic seed-owned mock channel
    # connections. Delete both ownership shapes before deleting the connections.
    #
    # Conversation deletion cascades messages, handoffs and persisted flow state,
    # preventing channel_connections from being left referenced by later tests.
    db.execute(
        delete(Conversation).where(
            Conversation.workspace_id == workspace_id,
            (
                Conversation.patient_id.in_(patient_ids)
                | Conversation.channel_connection_id.in_(connection_ids)
            ),
        )
    )
    db.execute(
        delete(Appointment).where(
            Appointment.workspace_id == workspace_id,
            Appointment.patient_id.in_(patient_ids),
        )
    )
    db.execute(
        delete(Lead).where(
            Lead.workspace_id == workspace_id,
            Lead.patient_id.in_(patient_ids),
        )
    )
    db.execute(
        delete(Patient).where(
            Patient.workspace_id == workspace_id,
            Patient.id.in_(patient_ids),
        )
    )
    db.execute(
        delete(ChannelConnection).where(
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.id.in_(connection_ids),
        )
    )
    db.execute(
        delete(AutomationWorker).where(
            AutomationWorker.workspace_id == workspace_id,
            AutomationWorker.id == sid(workspace_id, "automation-worker:mock"),
        )
    )
    db.flush()


def create_patients(db: Session, workspace: Workspace, branch: Branch) -> dict[str, Patient]:
    definitions = {
        "active": ("أحمد", "نشط", "+200000120001", "website", "active"),
        "inactive": ("منى", "غير نشطة", "+200000120002", "referral", "inactive"),
        "blocked": ("عميل", "محظور", "+200000120003", "whatsapp", "blocked"),
        "lead_new": ("ليد", "جديد", "+200000120004", "campaign", "active"),
        "lead_qualified": ("ليد", "مؤهل", "+200000120005", "instagram", "active"),
        "lead_lost": ("ليد", "مفقود", "+200000120006", "facebook", "active"),
        "booking_pending": ("حجز", "Pending", "+200000120007", "phone", "active"),
        "booking_confirmed": ("حجز", "Confirmed", "+200000120008", "whatsapp", "active"),
        "booking_policy_cancel": ("حجز", "Policy", "+200000120009", "website", "active"),
        "booking_lifecycle": ("حجز", "Lifecycle", "+200000120010", "walk_in", "active"),
        "booking_reschedule": ("حجز", "Reschedule", "+200000120011", "website", "active"),
        "booking_idempotent": ("حجز", "Idempotent", "+200000120012", "other", "active"),
        "automation_success": ("Automation", "Success", "+200000120013", "whatsapp", "active"),
        "automation_no_route": ("Automation", "NoRoute", "+200000120014", "website", "active"),
        "handoff_medical": ("هاندوف", "طبي", "+200000120015", "website", "active"),
        "handoff_complaint": ("هاندوف", "شكوى", "+200000120016", "whatsapp", "active"),
        "handoff_resolved": ("هاندوف", "محلول", "+200000120017", "email", "active"),
        "channel": ("واتساب", "ريجريشن", "+200000120018", "whatsapp", "active"),
        "agent_booking": ("Agent", "Booking", "+200000120019", "website", "active"),
    }
    result: dict[str, Patient] = {}
    for key, (first, last, phone, source, status) in definitions.items():
        patient = Patient(
            id=sid(workspace.id, f"patient:{key}"),
            workspace_id=workspace.id,
            first_name=first,
            last_name=last,
            phone=phone,
            phone_normalized=phone,
            preferred_language="ar",
            preferred_branch_id=branch.id,
            source=source,
            source_detail=SEED_MARKER,
            status=status,
            marketing_consent=key in {"active", "channel"},
        )
        db.add(patient)
        result[key] = patient
    db.flush()
    return result


def create_crm_data(
    db: Session,
    workspace: Workspace,
    patients: dict[str, Patient],
    service: Service,
    admin_user: User,
) -> None:
    tag_vip = upsert_by_id(
        db,
        PatientTag,
        sid(workspace.id, "tag:vip-regression"),
        workspace_id=workspace.id,
        name="VIP Regression",
        normalized_name="vip regression",
        color="#0f766e",
        is_active=True,
    )
    tag_follow = upsert_by_id(
        db,
        PatientTag,
        sid(workspace.id, "tag:followup-regression"),
        workspace_id=workspace.id,
        name="Follow-up Regression",
        normalized_name="follow-up regression",
        color="#f59e0b",
        is_active=True,
    )
    for key, tag in (("active", tag_vip), ("lead_qualified", tag_follow)):
        db.add(
            PatientTagAssignment(
                id=sid(workspace.id, f"tag-assignment:{key}:{tag.id}"),
                workspace_id=workspace.id,
                patient_id=patients[key].id,
                tag_id=tag.id,
                created_by_user_id=admin_user.id,
            )
        )

    for key, note_type, pinned, text in (
        ("active", "preference", True, "يفضل المواعيد المسائية — regression."),
        ("active", "customer_service", False, "ملاحظة خدمة عملاء staging."),
        ("lead_qualified", "follow_up", True, "Follow up tomorrow — regression."),
    ):
        db.add(
            PatientNote(
                id=sid(workspace.id, f"note:{key}:{note_type}"),
                workspace_id=workspace.id,
                patient_id=patients[key].id,
                author_user_id=admin_user.id,
                note_type=note_type,
                content=text,
                is_pinned=pinned,
            )
        )

    now = datetime.now(timezone.utc)
    leads = (
        ("new", "lead_new", "new", None, None),
        ("qualified", "lead_qualified", "qualified", 150000, now + timedelta(hours=4)),
        ("lost", "lead_lost", "lost", 250000, None),
        ("booked", "booking_pending", "booked", 150000, None),
    )
    for key, patient_key, status, value, follow in leads:
        db.add(
            Lead(
                id=sid(workspace.id, f"lead:{key}"),
                workspace_id=workspace.id,
                patient_id=patients[patient_key].id,
                service_id=service.id,
                assigned_user_id=admin_user.id if key in {"qualified", "booked"} else None,
                source=patients[patient_key].source,
                status=status,
                estimated_value_minor=value,
                currency="EGP",
                lost_reason="اختار عيادة تانية — regression" if status == "lost" else None,
                next_follow_up_at=follow,
                last_contact_at=now - timedelta(hours=1),
            )
        )


def add_history(
    db: Session,
    workspace_id: UUID,
    appointment: Appointment,
    key: str,
    from_status: str | None,
    to_status: str,
    reason: str,
    admin_user_id: UUID | None,
    created_at: datetime,
) -> None:
    db.add(
        AppointmentStatusHistory(
            id=sid(workspace_id, f"appointment-history:{key}"),
            workspace_id=workspace_id,
            appointment_id=appointment.id,
            changed_by_user_id=admin_user_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            metadata_json={"seed": SEED_MARKER},
            created_at=created_at,
        )
    )


def appointment(
    *,
    workspace: Workspace,
    patients: dict[str, Patient],
    key: str,
    patient_key: str,
    branch: Branch,
    doctor: Doctor,
    service: Service,
    status: str,
    start: datetime,
    source: str = "staff",
    idempotency_key: str | None = None,
    rescheduled_from: UUID | None = None,
) -> Appointment:
    duration = service.duration_minutes
    end = start + timedelta(minutes=duration)
    now = datetime.now(timezone.utc)
    row = Appointment(
        id=sid(workspace.id, f"appointment:{key}"),
        workspace_id=workspace.id,
        patient_id=patients[patient_key].id,
        branch_id=branch.id,
        doctor_id=doctor.id,
        service_id=service.id,
        status=status,
        source=source,
        start_at=start,
        end_at=end,
        busy_start_at=start - timedelta(minutes=service.buffer_before_minutes),
        busy_end_at=end + timedelta(minutes=service.buffer_after_minutes),
        duration_minutes=duration,
        price_minor=service.price_minor,
        currency=service.currency,
        customer_note=f"{SEED_MARKER}:{key}",
        idempotency_key=idempotency_key,
        rescheduled_from_appointment_id=rescheduled_from,
        created_at=now,
        updated_at=now,
    )
    if status == "confirmed":
        row.confirmed_at = now
    elif status == "cancelled":
        row.cancelled_at = now - timedelta(hours=1)
        row.cancellation_reason = "Regression seeded cancellation"
    elif status == "completed":
        row.confirmed_at = now - timedelta(days=2)
        row.completed_at = now - timedelta(days=1)
    elif status == "no_show":
        row.confirmed_at = now - timedelta(days=2)
        row.no_show_at = now - timedelta(days=1)
    return row


def create_appointments(
    db: Session,
    workspace: Workspace,
    patients: dict[str, Patient],
    branch_main: Branch,
    branch_second: Branch,
    doctor_main: Doctor,
    doctor_second: Doctor,
    laser: Service,
    facial: Service,
    admin_user: User,
) -> dict[str, Appointment]:
    now = datetime.now(timezone.utc)
    rows = {
        "pending": appointment(
            workspace=workspace, patients=patients, key="pending", patient_key="booking_pending",
            branch=branch_main, doctor=doctor_main, service=laser, status="pending",
            start=local_at(2, 11, 0), source="ai",
        ),
        "confirmed": appointment(
            workspace=workspace, patients=patients, key="confirmed", patient_key="booking_confirmed",
            branch=branch_main, doctor=doctor_main, service=laser, status="confirmed",
            start=local_at(2, 13, 0), source="whatsapp",
        ),
        "policy_cancel": appointment(
            workspace=workspace, patients=patients, key="policy_cancel", patient_key="booking_policy_cancel",
            branch=branch_second, doctor=doctor_second, service=facial, status="confirmed",
            start=now + timedelta(hours=6), source="website",
        ),
        "lifecycle": appointment(
            workspace=workspace, patients=patients, key="lifecycle", patient_key="booking_lifecycle",
            branch=branch_second, doctor=doctor_second, service=facial, status="confirmed",
            start=local_at(3, 14, 0), source="walk_in",
        ),
        "reschedule_source": appointment(
            workspace=workspace, patients=patients, key="reschedule_source", patient_key="booking_reschedule",
            branch=branch_main, doctor=doctor_main, service=laser, status="pending",
            start=local_at(5, 12, 0), source="website",
        ),
        "idempotent": appointment(
            workspace=workspace, patients=patients, key="idempotent", patient_key="booking_idempotent",
            branch=branch_main, doctor=doctor_main, service=laser, status="pending",
            start=local_at(6, 11, 0), source="staff",
            idempotency_key="staging-regression-idempotency-v1",
        ),
        "completed": appointment(
            workspace=workspace, patients=patients, key="completed", patient_key="active",
            branch=branch_main, doctor=doctor_main, service=laser, status="completed",
            start=local_at(-3, 12, 0),
        ),
        "cancelled": appointment(
            workspace=workspace, patients=patients, key="cancelled", patient_key="active",
            branch=branch_main, doctor=doctor_main, service=laser, status="cancelled",
            start=local_at(-2, 15, 0),
        ),
        "no_show": appointment(
            workspace=workspace, patients=patients, key="no_show", patient_key="active",
            branch=branch_main, doctor=doctor_main, service=laser, status="no_show",
            start=local_at(-1, 16, 0),
        ),
        "checked_in": appointment(
            workspace=workspace, patients=patients, key="checked_in", patient_key="active",
            branch=branch_main, doctor=doctor_main, service=facial, status="checked_in",
            start=now - timedelta(hours=4),
        ),
        "in_progress": appointment(
            workspace=workspace, patients=patients, key="in_progress", patient_key="active",
            branch=branch_second, doctor=doctor_second, service=facial, status="in_progress",
            start=now - timedelta(hours=2),
        ),
        "rescheduled_old": appointment(
            workspace=workspace, patients=patients, key="rescheduled_old", patient_key="active",
            branch=branch_main, doctor=doctor_main, service=laser, status="rescheduled",
            start=local_at(7, 15, 0),
        ),
        "automation_success": appointment(
            workspace=workspace, patients=patients, key="automation_success", patient_key="automation_success",
            branch=branch_main, doctor=doctor_main, service=laser, status="completed",
            start=local_at(-3, 10, 0), source="whatsapp",
        ),
        "automation_no_route": appointment(
            workspace=workspace, patients=patients, key="automation_no_route", patient_key="automation_no_route",
            branch=branch_second, doctor=doctor_second, service=facial, status="completed",
            start=local_at(-3, 12, 0), source="website",
        ),
        "automation_cancelled": appointment(
            workspace=workspace, patients=patients, key="automation_cancelled", patient_key="automation_no_route",
            branch=branch_second, doctor=doctor_second, service=facial, status="cancelled",
            start=local_at(4, 15, 0), source="website",
        ),
    }
    old = rows["rescheduled_old"]
    new = appointment(
        workspace=workspace, patients=patients, key="rescheduled_new", patient_key="active",
        branch=branch_main, doctor=doctor_main, service=laser, status="confirmed",
        start=local_at(7, 17, 0), rescheduled_from=old.id,
    )
    rows["rescheduled_new"] = new

    for row in rows.values():
        db.add(row)
    db.flush()

    for key, row in rows.items():
        add_history(
            db, workspace.id, row, key, None, row.status,
            f"seed_{row.status}", admin_user.id, now - timedelta(minutes=30)
        )
    add_history(
        db, workspace.id, old, "rescheduled-old-transition", "confirmed", "rescheduled",
        "Regression seeded reschedule", admin_user.id, now - timedelta(minutes=20)
    )
    return rows


def create_channels(
    db: Session,
    workspace: Workspace,
    patients: dict[str, Patient],
    admin_user: User,
    now: datetime,
) -> tuple[ChannelConnection, ChannelConnection, dict[str, Conversation]]:
    connection = ChannelConnection(
        id=sid(workspace.id, "channel:mock-whatsapp"),
        workspace_id=workspace.id,
        channel="whatsapp",
        provider="staging_mock",
        display_name="Regression Mock WhatsApp",
        status="active",
        external_account_id="staging-phone-number-id",
        adapter_token_hash=hashlib.sha256(MOCK_CHANNEL_TOKEN.encode()).hexdigest(),
        created_by_user_id=admin_user.id,
        config_json={"seed": SEED_MARKER, "mock": True, "do_not_send": True},
    )
    paused = ChannelConnection(
        id=sid(workspace.id, "channel:paused-instagram"),
        workspace_id=workspace.id,
        channel="instagram",
        provider="staging_mock",
        display_name="Regression Paused Instagram",
        status="paused",
        external_account_id="staging-instagram-id",
        adapter_token_hash=hashlib.sha256(MOCK_PAUSED_CHANNEL_TOKEN.encode()).hexdigest(),
        created_by_user_id=admin_user.id,
        config_json={"seed": SEED_MARKER, "mock": True, "do_not_send": True},
    )
    db.add_all([connection, paused])
    db.flush()

    identity_keys = ("channel", "handoff_complaint", "automation_success", "agent_booking")
    for key in identity_keys:
        db.add(
            ChannelIdentity(
                id=sid(workspace.id, f"channel-identity:{key}"),
                workspace_id=workspace.id,
                channel_connection_id=connection.id,
                patient_id=patients[key].id,
                external_user_id=patients[key].phone_normalized.lstrip("+"),
                display_name=f"{patients[key].first_name} {patients[key].last_name}",
                phone=patients[key].phone,
                metadata_json={"seed": SEED_MARKER},
            )
        )
    db.flush()

    conversations: dict[str, Conversation] = {}
    definitions = {
        "web_open": ("active", "web", "open", None),
        "web_closed": ("active", "web", "closed", None),
        "handoff_medical": ("handoff_medical", "web", "pending", None),
        "handoff_complaint": ("handoff_complaint", "whatsapp", "pending", connection.id),
        "handoff_resolved": ("handoff_resolved", "email", "open", None),
        "whatsapp_open": ("channel", "whatsapp", "open", connection.id),
    }
    for idx, (key, (patient_key, channel, status, connection_id)) in enumerate(definitions.items()):
        conv = Conversation(
            id=sid(workspace.id, f"conversation:{key}"),
            workspace_id=workspace.id,
            patient_id=patients[patient_key].id,
            channel=channel,
            status=status,
            external_conversation_id=f"staging-{key}" if connection_id else None,
            channel_connection_id=connection_id,
            assigned_user_id=admin_user.id if key == "handoff_complaint" else None,
            subject=f"Regression {key}",
            started_at=now - timedelta(hours=idx + 1),
            last_message_at=now - timedelta(minutes=idx * 5 + 1),
            closed_at=now - timedelta(minutes=30) if status == "closed" else None,
        )
        db.add(conv)
        conversations[key] = conv
    db.flush()

    def msg(
        key: str,
        conv_key: str,
        sender: str,
        direction: str,
        content: str,
        delivery: str,
        channel_conn: UUID | None = None,
        external_message_id: str | None = None,
        minutes_ago: int = 0,
        sent_by: UUID | None = None,
        message_type: str = "text",
    ) -> Message:
        row = Message(
            id=sid(workspace.id, f"message:{key}"),
            workspace_id=workspace.id,
            conversation_id=conversations[conv_key].id,
            channel_connection_id=channel_conn,
            sender_type=sender,
            direction=direction,
            message_type=message_type,
            content=content,
            external_message_id=external_message_id,
            delivery_status=delivery,
            sent_by_user_id=sent_by,
            metadata_json={"seed": SEED_MARKER, "scenario": key},
            created_at=now - timedelta(minutes=minutes_ago),
            updated_at=now - timedelta(minutes=minutes_ago),
        )
        db.add(row)
        return row

    msg("web-open-in", "web_open", "patient", "inbound", "عايزة أعرف الأسعار", "received", minutes_ago=20)
    msg("web-open-ai", "web_open", "ai", "outbound", "أكيد، أقدر أساعدك.", "sent", minutes_ago=19)
    msg("web-closed-in", "web_closed", "patient", "inbound", "شكراً", "received", minutes_ago=50)

    medical_in = msg(
        "medical-in", "handoff_medical", "patient", "inbound",
        "أنا حامل، ينفع أعمل ليزر؟", "received", minutes_ago=15,
    )
    msg(
        "medical-ai", "handoff_medical", "ai", "outbound",
        "الموضوع ده محتاج تقييم من الفريق الطبي.", "sent", minutes_ago=14,
    )
    complaint_in = msg(
        "complaint-in", "handoff_complaint", "patient", "inbound",
        "عندي شكوى ومحتاج حد من العيادة.", "received",
        channel_conn=connection.id, external_message_id="wamid.staging.complaint.in", minutes_ago=12,
    )
    msg(
        "resolved-in", "handoff_resolved", "patient", "inbound",
        "كنت محتاج مساعدة واتحلت.", "received", minutes_ago=60,
    )

    wa_in = msg(
        "wa-in-processed", "whatsapp_open", "patient", "inbound",
        "عايز أعرف سعر الليزر", "received",
        channel_conn=connection.id, external_message_id="wamid.staging.in.processed", minutes_ago=10,
    )
    wa_ai = msg(
        "wa-out-read", "whatsapp_open", "ai", "outbound",
        "سعر الليزر 1500 جنيه في سيناريو regression.", "read",
        channel_conn=connection.id, external_message_id=None, minutes_ago=9,
    )
    wa_queued = msg(
        "wa-out-queued", "whatsapp_open", "staff", "outbound",
        "رسالة queued لاختبار الـOutbox.", "queued",
        channel_conn=connection.id, minutes_ago=8, sent_by=admin_user.id,
    )
    wa_failed = msg(
        "wa-out-failed", "whatsapp_open", "system", "outbound",
        "رسالة failed لاختبار retry.", "failed",
        channel_conn=connection.id, minutes_ago=7,
    )

    db.flush()

    db.add_all([
        ChannelInboundEvent(
            id=sid(workspace.id, "channel-event:processed"),
            workspace_id=workspace.id,
            channel_connection_id=connection.id,
            message_id=wa_in.id,
            outbound_message_id=wa_ai.id,
            external_event_id="staging-event-processed",
            status="processed",
            attempts=1,
            payload_json={"seed": SEED_MARKER},
        ),
        ChannelInboundEvent(
            id=sid(workspace.id, "channel-event:failed"),
            workspace_id=workspace.id,
            channel_connection_id=connection.id,
            message_id=complaint_in.id,
            outbound_message_id=None,
            external_event_id="staging-event-failed",
            status="failed",
            attempts=2,
            last_error="Synthetic staging failure",
            payload_json={"seed": SEED_MARKER},
        ),
    ])

    read_dispatch = MessageDispatch(
        id=sid(workspace.id, "dispatch:read"),
        workspace_id=workspace.id,
        channel_connection_id=connection.id,
        message_id=wa_ai.id,
        status="read",
        attempts=1,
        provider_message_id="wamid.staging.read",
        sent_at=now - timedelta(minutes=9),
        delivered_at=now - timedelta(minutes=8),
        read_at=now - timedelta(minutes=7),
        metadata_json={"seed": SEED_MARKER},
    )
    queued_dispatch = MessageDispatch(
        id=sid(workspace.id, "dispatch:queued"),
        workspace_id=workspace.id,
        channel_connection_id=connection.id,
        message_id=wa_queued.id,
        status="queued",
        attempts=0,
        metadata_json={"seed": SEED_MARKER},
    )
    failed_dispatch = MessageDispatch(
        id=sid(workspace.id, "dispatch:failed"),
        workspace_id=workspace.id,
        channel_connection_id=connection.id,
        message_id=wa_failed.id,
        status="failed",
        attempts=2,
        last_error="Synthetic provider error",
        next_attempt_at=now + timedelta(minutes=10),
        metadata_json={"seed": SEED_MARKER},
    )
    db.add_all([read_dispatch, queued_dispatch, failed_dispatch])
    db.flush()

    db.add_all([
        ChannelDeliveryEvent(
            id=sid(workspace.id, "delivery-event:sent"),
            workspace_id=workspace.id,
            channel_connection_id=connection.id,
            provider_message_id="wamid.staging.read",
            external_event_id="staging-delivery-sent",
            status="sent",
            occurred_at=now - timedelta(minutes=9),
            processed_at=now - timedelta(minutes=9),
            payload_json={"seed": SEED_MARKER},
        ),
        ChannelDeliveryEvent(
            id=sid(workspace.id, "delivery-event:delivered"),
            workspace_id=workspace.id,
            channel_connection_id=connection.id,
            provider_message_id="wamid.staging.read",
            external_event_id="staging-delivery-delivered",
            status="delivered",
            occurred_at=now - timedelta(minutes=8),
            processed_at=now - timedelta(minutes=8),
            payload_json={"seed": SEED_MARKER},
        ),
        ChannelDeliveryEvent(
            id=sid(workspace.id, "delivery-event:read"),
            workspace_id=workspace.id,
            channel_connection_id=connection.id,
            provider_message_id="wamid.staging.read",
            external_event_id="staging-delivery-read",
            status="read",
            occurred_at=now - timedelta(minutes=7),
            processed_at=now - timedelta(minutes=7),
            payload_json={"seed": SEED_MARKER},
        ),
    ])
    return connection, paused, conversations


def create_handoffs(
    db: Session,
    workspace: Workspace,
    patients: dict[str, Patient],
    conversations: dict[str, Conversation],
    admin_user: User,
    now: datetime,
) -> dict[str, HandoffRequest]:
    medical = HandoffRequest(
        id=sid(workspace.id, "handoff:medical_pending"),
        workspace_id=workspace.id,
        conversation_id=conversations["handoff_medical"].id,
        patient_id=patients["handoff_medical"].id,
        status="pending",
        category="medical",
        priority="high",
        source="ai",
        reason="Pregnancy + laser safety regression scenario.",
    )
    complaint = HandoffRequest(
        id=sid(workspace.id, "handoff:complaint_claimed"),
        workspace_id=workspace.id,
        conversation_id=conversations["handoff_complaint"].id,
        patient_id=patients["handoff_complaint"].id,
        status="claimed",
        category="complaint",
        priority="urgent",
        source="customer",
        reason="Customer complaint regression scenario.",
        assigned_user_id=admin_user.id,
        claimed_at=now - timedelta(minutes=10),
    )
    resolved = HandoffRequest(
        id=sid(workspace.id, "handoff:customer_resolved"),
        workspace_id=workspace.id,
        conversation_id=conversations["handoff_resolved"].id,
        patient_id=patients["handoff_resolved"].id,
        status="resolved",
        category="customer_request",
        priority="normal",
        source="staff",
        reason="Resolved customer-request regression scenario.",
        assigned_user_id=admin_user.id,
        claimed_at=now - timedelta(hours=2),
        resolved_at=now - timedelta(hours=1),
        resolved_by_user_id=admin_user.id,
        resolution_note="Resolved in seeded staging scenario.",
    )
    db.add_all([medical, complaint, resolved])
    db.flush()
    for key, handoff, event_type, actor_type in (
        ("medical-created", medical, "created", "ai"),
        ("complaint-created", complaint, "created", "system"),
        ("complaint-claimed", complaint, "claimed", "staff"),
        ("resolved-created", resolved, "created", "staff"),
        ("resolved-resolved", resolved, "resolved", "staff"),
    ):
        db.add(
            HandoffEvent(
                id=sid(workspace.id, f"handoff-event:{key}"),
                workspace_id=workspace.id,
                handoff_request_id=handoff.id,
                conversation_id=handoff.conversation_id,
                event_type=event_type,
                actor_type=actor_type,
                actor_user_id=admin_user.id if actor_type == "staff" else None,
                metadata_json={"seed": SEED_MARKER},
            )
        )
    return {
        "medical_pending": medical,
        "complaint_claimed": complaint,
        "customer_resolved": resolved,
    }


def ensure_automation_rules(db: Session, workspace: Workspace) -> dict[str, AutomationRule]:
    rows = {
        row.key: row
        for row in db.scalars(
            select(AutomationRule).where(AutomationRule.workspace_id == workspace.id)
        )
    }

    # Keep the clinic's existing rule settings untouched.
    for definition in DEFAULT_AUTOMATION_RULES:
        row = rows.get(definition.key)
        if row is None:
            row = AutomationRule(
                id=sid(workspace.id, f"automation-rule:{definition.key}"),
                workspace_id=workspace.id,
                key=definition.key,
                name=definition.name,
                enabled=False,
                trigger_kind=definition.trigger_kind,
                offset_minutes=definition.offset_minutes,
                channel=definition.channel,
                template_name=definition.template_name,
                template_language=definition.template_language,
                max_lateness_minutes=definition.max_lateness_minutes,
                config_json={},
            )
            db.add(row)
            rows[definition.key] = row

    regression_key = "staging_regression_booking_confirmation"
    regression = rows.get(regression_key)
    if regression is None:
        regression = AutomationRule(
            id=sid(workspace.id, "automation-rule:staging-regression"),
            workspace_id=workspace.id,
            key=regression_key,
            name="STAGING Regression Booking Confirmation",
            enabled=True,
            trigger_kind="after_completed",
            offset_minutes=-10080,
            channel="whatsapp",
            template_name="tia_staging_regression_do_not_send",
            template_language="ar",
            max_lateness_minutes=0,
            config_json={"seed": SEED_MARKER, "mock_only": True},
        )
        db.add(regression)
        rows[regression_key] = regression
    else:
        regression.enabled = True
        regression.trigger_kind = "after_completed"
        regression.offset_minutes = -10080
        regression.channel = "whatsapp"
        regression.template_name = "tia_staging_regression_do_not_send"
        regression.template_language = "ar"
        regression.max_lateness_minutes = 0
        regression.config_json = {"seed": SEED_MARKER, "mock_only": True}

    db.flush()
    return rows


def create_automation_data(
    db: Session,
    workspace: Workspace,
    appointments: dict[str, Appointment],
    patients: dict[str, Patient],
    rules: dict[str, AutomationRule],
    admin_user: User,
    now: datetime,
) -> AutomationWorker:
    worker = AutomationWorker(
        id=sid(workspace.id, "automation-worker:mock"),
        workspace_id=workspace.id,
        name="Regression n8n Mock Worker",
        token_hash=hashlib.sha256(MOCK_AUTOMATION_TOKEN.encode()).hexdigest(),
        status="active",
        created_by_user_id=admin_user.id,
        last_seen_at=now - timedelta(minutes=5),
    )
    db.add(worker)

    booking_rule = rules["staging_regression_booking_confirmation"]
    job_defs = (
        ("success_processing", "automation_success", "automation_success", "processing"),
        ("no_route_processing", "automation_no_route", "automation_no_route", "processing"),
        ("cancelled_target_processing", "automation_cancelled", "automation_no_route", "processing"),
        ("historical_dispatched", "confirmed", "booking_confirmed", "dispatched"),
        ("historical_failed", "pending", "booking_pending", "failed"),
        ("historical_skipped", "completed", "active", "skipped"),
        ("historical_cancelled", "cancelled", "active", "cancelled"),
    )
    for idx, (key, appointment_key, patient_key, status) in enumerate(job_defs):
        db.add(
            AutomationJob(
                id=sid(workspace.id, f"automation-job:{key}"),
                workspace_id=workspace.id,
                rule_id=booking_rule.id,
                appointment_id=appointments[appointment_key].id,
                patient_id=patients[patient_key].id,
                status=status,
                scheduled_for=now - timedelta(minutes=idx + 1),
                dedupe_key=f"staging-regression:{key}",
                attempts=1 if status != "failed" else 2,
                locked_at=now if status == "processing" else None,
                next_attempt_at=now - timedelta(seconds=1) if status == "failed" else None,
                last_error="Synthetic automation failure" if status == "failed" else None,
                payload_json={"seed": SEED_MARKER, "scenario": key},
                result_json={"seeded_status": status},
                completed_at=now - timedelta(minutes=1) if status in {"dispatched", "skipped", "cancelled"} else None,
            )
        )
    return worker


def main() -> int:
    require_staging()

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)

    with SessionLocal() as db:
        primary_workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if primary_workspace is None:
            print("Workspace slug 'tia' was not found.", file=sys.stderr)
            return 1

        admin_membership = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == primary_workspace.id,
                WorkspaceMember.role == "admin",
                WorkspaceMember.is_active.is_(True),
            ).limit(1)
        )
        if admin_membership is None:
            print("No active admin exists in the Linka workspace.", file=sys.stderr)
            return 1
        admin_user = db.get(User, admin_membership.user_id)
        if admin_user is None:
            print("Admin user record is missing.", file=sys.stderr)
            return 1

        workspace = db.get(Workspace, REGRESSION_WORKSPACE_ID)
        if workspace is None:
            slug_conflict = db.scalar(
                select(Workspace).where(Workspace.slug == REGRESSION_WORKSPACE_SLUG)
            )
            if slug_conflict is not None:
                print(
                    f"Regression workspace slug {REGRESSION_WORKSPACE_SLUG!r} exists with an unexpected id.",
                    file=sys.stderr,
                )
                return 1
            workspace = Workspace(
                id=REGRESSION_WORKSPACE_ID,
                name="Linka Regression",
                slug=REGRESSION_WORKSPACE_SLUG,
                timezone=primary_workspace.timezone,
                is_active=True,
            )
            db.add(workspace)
            db.flush()
        else:
            workspace.name = "Linka Regression"
            workspace.slug = REGRESSION_WORKSPACE_SLUG
            workspace.timezone = primary_workspace.timezone
            workspace.is_active = True

        regression_membership = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == admin_user.id,
            )
        )
        if regression_membership is None:
            db.add(
                WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=admin_user.id,
                    role="admin",
                    is_active=True,
                )
            )
        else:
            regression_membership.role = "admin"
            regression_membership.is_active = True
        db.flush()

        try:
            cleanup_seed_owned(db, workspace.id)
            branch_main, branch_second, doctor_main, doctor_second, laser, botox, facial = ensure_clinic(db, workspace)
            patients = create_patients(db, workspace, branch_main)
            create_crm_data(db, workspace, patients, laser, admin_user)
            appointments = create_appointments(
                db, workspace, patients, branch_main, branch_second,
                doctor_main, doctor_second, laser, facial, admin_user,
            )
            now = datetime.now(timezone.utc)
            channel, paused, conversations = create_channels(
                db, workspace, patients, admin_user, now
            )
            handoffs = create_handoffs(
                db, workspace, patients, conversations, admin_user, now
            )
            rules = ensure_automation_rules(db, workspace)
            worker = create_automation_data(
                db, workspace, appointments, patients, rules, admin_user, now
            )
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            print("Seed failed due to a database constraint:", file=sys.stderr)
            print(exc, file=sys.stderr)
            return 1

        summary = {
            "seed_version": SEED_VERSION,
            "environment": settings.environment,
            "workspace_id": str(workspace.id),
            "admin_user_id": str(admin_user.id),
            "branch_ids": {
                "main": str(branch_main.id),
                "secondary": str(branch_second.id),
            },
            "doctor_ids": {
                "main": str(doctor_main.id),
                "secondary": str(doctor_second.id),
            },
            "service_ids": {
                "laser": str(laser.id),
                "botox": str(botox.id),
                "facial": str(facial.id),
            },
            "patient_ids": {
                key: str(patient.id) for key, patient in patients.items()
            },
            "appointment_ids": {
                key: str(row.id) for key, row in appointments.items()
            },
            "conversation_ids": {
                key: str(row.id) for key, row in conversations.items()
            },
            "handoff_ids": {
                key: str(row.id) for key, row in handoffs.items()
            },
            "channel_connection_id": str(channel.id),
            "paused_channel_connection_id": str(paused.id),
            "automation_worker_id": str(worker.id),
            "safety": {
                "mock_provider_only": True,
                "external_messages_sent": False,
                "production_blocked": True,
            },
        }
        print("Linka full staging regression data is ready")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print()
        print("Mock adapter tokens are deterministic staging-only values used by the regression runner.")
        print("No Meta/WhatsApp credential is stored or used by this seed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
