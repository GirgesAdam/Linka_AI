from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.appointment import Appointment
from app.models.automation_job import AutomationJob
from app.models.automation_rule import AutomationRule
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.channel_identity import ChannelIdentity
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.handoff_request import HandoffRequest
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from final_gate_scenarios import GATE_MARKER, gate_ids
from staging_scenarios import sid


SYNTHETIC_PHONE_PREFIX = "+200000"
PRIMARY_PATIENT_FIXTURES = (
    ("race_patient_a", "محمود", "سامح", "+200000000001", "website"),
    ("race_patient_b", "سلمى", "عادل", "+200000000002", "website"),
    ("member_patient", "ياسمين", "خالد", "+200000000003", "website"),
    ("automation_reschedule_patient", "نورهان", "شريف", "+200000000004", "website"),
    ("automation_cancel_patient", "كريم", "وائل", "+200000000005", "website"),
    ("channel_patient", "هند", "مصطفى", "+200000000006", "whatsapp"),
)


def _utc_future(days: int, hour: int) -> datetime:
    cairo = ZoneInfo("Africa/Cairo")
    local = datetime.now(cairo).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(days=days)
    return local.astimezone(timezone.utc)


def _cleanup_primary(db: Session, workspace_id: UUID, ids: dict[str, UUID]) -> None:
    patient_ids = [
        ids["race_patient_a"],
        ids["race_patient_b"],
        ids["member_patient"],
        ids["automation_reschedule_patient"],
        ids["automation_cancel_patient"],
        ids["channel_patient"],
    ]
    db.execute(
        delete(AutomationJob).where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.patient_id.in_(patient_ids),
        )
    )
    db.execute(
        delete(Appointment).where(
            Appointment.workspace_id == workspace_id,
            Appointment.patient_id.in_(patient_ids),
        )
    )
    db.execute(
        delete(HandoffRequest).where(
            HandoffRequest.workspace_id == workspace_id,
            HandoffRequest.id == ids["channel_handoff"],
        )
    )
    db.execute(
        delete(Conversation).where(
            Conversation.workspace_id == workspace_id,
            Conversation.patient_id.in_(patient_ids),
        )
    )
    db.execute(
        delete(Patient).where(
            Patient.workspace_id == workspace_id,
            Patient.id.in_(patient_ids),
        )
    )
    db.execute(
        delete(AutomationRule).where(
            AutomationRule.workspace_id == workspace_id,
            AutomationRule.id == ids["automation_rule"],
        )
    )


def _seed_primary(db: Session, workspace: Workspace, ids: dict[str, UUID]) -> None:
    connection_id = sid(workspace.id, "channel:mock-whatsapp")
    if db.get(Branch, sid(workspace.id, "branch:regression-main")) is None:
        raise RuntimeError(
            "Full staging fixtures are missing. Run seed_full_staging_demo.py first."
        )

    patients = [
        Patient(
            id=ids[key],
            workspace_id=workspace.id,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            phone_normalized=phone,
            preferred_language="ar",
            source=source,
            source_detail=GATE_MARKER,
            status="active",
            marketing_consent=False,
        )
        for key, first_name, last_name, phone, source in PRIMARY_PATIENT_FIXTURES
    ]
    db.add_all(patients)
    db.flush()

    connection = db.scalar(
        select(ChannelConnection).where(
            ChannelConnection.workspace_id == workspace.id,
            ChannelConnection.id == connection_id,
        )
    )
    if connection is None:
        raise RuntimeError("Mock WhatsApp channel from full staging seed was not found.")

    conv = Conversation(
        id=ids["channel_conversation"],
        workspace_id=workspace.id,
        patient_id=ids["channel_patient"],
        channel="whatsapp",
        status="pending",
        external_conversation_id="tia-final-gate-channel-conversation",
        channel_connection_id=connection_id,
        subject="طلب تحويل لموظف — اختبار داخلي",
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
    )
    db.add(conv)
    db.flush()

    db.add(
        ChannelIdentity(
            id=ids["channel_identity"],
            workspace_id=workspace.id,
            channel_connection_id=connection_id,
            patient_id=ids["channel_patient"],
            external_user_id="tia-final-gate-channel-user",
            display_name="هند مصطفى",
            phone="+200000000006",
            metadata_json={"marker": GATE_MARKER},
        )
    )
    db.add(
        HandoffRequest(
            id=ids["channel_handoff"],
            workspace_id=workspace.id,
            conversation_id=ids["channel_conversation"],
            patient_id=ids["channel_patient"],
            status="pending",
            category="customer_request",
            priority="normal",
            source="system",
            reason="اختبار داخلي: إبقاء رد الذكاء الاصطناعي متوقفًا أثناء التحويل لموظف.",
        )
    )

    outbound = Message(
        id=ids["provider_message"],
        workspace_id=workspace.id,
        conversation_id=ids["channel_conversation"],
        channel_connection_id=connection_id,
        sender_type="staff",
        direction="outbound",
        message_type="text",
        content="رسالة اختبار داخلية للتحقق من حالة التسليم.",
        external_message_id="tia-final-gate-provider-message",
        delivery_status="sent",
        metadata_json={"marker": GATE_MARKER},
    )
    db.add(outbound)
    db.flush()
    db.add(
        MessageDispatch(
            id=ids["provider_dispatch"],
            workspace_id=workspace.id,
            channel_connection_id=connection_id,
            message_id=outbound.id,
            status="sent",
            attempts=1,
            provider_message_id="tia-final-gate-provider-message",
            sent_at=datetime.now(timezone.utc),
            metadata_json={"marker": GATE_MARKER},
        )
    )

    db.add(
        AutomationRule(
            id=ids["automation_rule"],
            workspace_id=workspace.id,
            key="final_gate_reminder_24h",
            name="تذكير موعد قبل 24 ساعة — اختبار داخلي",
            enabled=True,
            trigger_kind="before_appointment",
            offset_minutes=-1440,
            channel="auto",
            template_name="tia_final_gate_reminder_24h",
            template_language="ar",
            max_lateness_minutes=120,
            config_json={"marker": GATE_MARKER},
        )
    )


def _seed_secondary(
    db: Session,
    *,
    primary_workspace_id: UUID,
    member_user: User,
    ids: dict[str, UUID],
) -> Workspace:
    secondary_id = ids["secondary_workspace"]
    existing = db.get(Workspace, secondary_id)
    if existing is not None:
        db.delete(existing)
        db.flush()

    workspace = Workspace(
        id=secondary_id,
        name="عيادة Linka التجريبية — Workspace B",
        slug=f"tia-final-gate-{str(primary_workspace_id)[:8]}",
        timezone="Africa/Cairo",
        is_active=True,
    )
    db.add(workspace)
    db.flush()
    db.add(
        WorkspaceMember(
            workspace_id=secondary_id,
            user_id=member_user.id,
            role="admin",
            is_active=True,
        )
    )

    branch = Branch(
        id=ids["secondary_branch"], workspace_id=secondary_id,
        name="فرع التجمع الخامس — اختبار داخلي", code="qa-new-cairo",
        city="Cairo", country_code="EG", timezone="Africa/Cairo", is_active=True,
    )
    staff = Staff(
        id=ids["secondary_staff"], workspace_id=secondary_id,
        first_name="سارة", last_name="منصور", job_title="طبيب", is_active=True,
    )
    doctor = Doctor(
        id=ids["secondary_doctor"], workspace_id=secondary_id,
        staff_id=ids["secondary_staff"], specialization="جلدية وتجميل",
        booking_enabled=True, is_active=True,
    )
    service = Service(
        id=ids["secondary_service"], workspace_id=secondary_id,
        name="ليزر إزالة الشعر — اختبار داخلي", slug="qa-laser-hair-removal",
        category="Laser", duration_minutes=60,
        buffer_before_minutes=0, buffer_after_minutes=0,
        price_minor=150000, currency="EGP", requires_medical_review=False,
        is_active=True,
    )
    db.add_all([branch, staff, service])
    db.flush()
    db.add(doctor)
    db.flush()
    db.add_all([
        DoctorBranch(
            id=ids["secondary_doctor_branch"], workspace_id=secondary_id,
            doctor_id=doctor.id, branch_id=branch.id, is_primary=True, is_active=True,
        ),
        DoctorService(
            id=ids["secondary_doctor_service"], workspace_id=secondary_id,
            doctor_id=doctor.id, service_id=service.id, is_active=True,
        ),
    ])
    db.flush()
    for weekday in range(7):
        db.add(BranchWorkingHour(
            workspace_id=secondary_id, branch_id=branch.id, weekday=weekday,
            start_time=time(10,0), end_time=time(22,0),
        ))
        db.add(DoctorWorkingHour(
            workspace_id=secondary_id, doctor_id=doctor.id, branch_id=branch.id,
            weekday=weekday, start_time=time(10,0), end_time=time(22,0),
        ))
    db.add(BookingSettings(
        workspace_id=secondary_id, slot_interval_minutes=15,
        minimum_notice_minutes=0, booking_horizon_days=90,
        cancellation_notice_minutes=0, allow_same_day_booking=True,
        require_confirmation=False, default_currency="EGP",
    ))

    patient = Patient(
        id=ids["secondary_patient"], workspace_id=secondary_id,
        first_name="ريم", last_name="حسام", phone="+200000000101",
        phone_normalized="+200000000101", preferred_language="ar",
        source="website", source_detail=GATE_MARKER,
        status="active", marketing_consent=False,
    )
    db.add(patient)
    db.flush()
    start = _utc_future(7, 13)
    appointment = Appointment(
        id=ids["secondary_appointment"], workspace_id=secondary_id,
        patient_id=patient.id, branch_id=branch.id, doctor_id=doctor.id,
        service_id=service.id, status="confirmed", source="staff",
        start_at=start, end_at=start+timedelta(minutes=60),
        busy_start_at=start, busy_end_at=start+timedelta(minutes=60),
        duration_minutes=60, price_minor=150000, currency="EGP",
        confirmed_at=datetime.now(timezone.utc),
    )
    conversation = Conversation(
        id=ids["secondary_conversation"], workspace_id=secondary_id,
        patient_id=patient.id, channel="web", status="open",
        subject="استفسار عن موعد — اختبار داخلي", started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
    )
    db.add_all([appointment, conversation])
    return workspace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--member-auth-user-id", required=True)
    parser.add_argument("--member-email", required=True)
    args = parser.parse_args()

    if settings.is_production:
        print("Refusing to seed final internal gate in production.", file=sys.stderr)
        return 2

    workspace_id = UUID(args.workspace_id)
    auth_user_id = UUID(args.member_auth_user_id)
    ids = gate_ids(workspace_id)

    with SessionLocal() as db:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            print("Primary workspace was not found.", file=sys.stderr)
            return 2

        _cleanup_primary(db, workspace_id, ids)
        member_user = db.scalar(select(User).where(User.email == args.member_email))
        if member_user is not None:
            db.execute(
                delete(WorkspaceMember).where(
                    WorkspaceMember.user_id == member_user.id,
                    WorkspaceMember.workspace_id.in_(
                        (workspace_id, ids["secondary_workspace"])
                    ),
                )
            )
            db.flush()

        secondary = db.get(Workspace, ids["secondary_workspace"])
        if secondary is not None:
            db.delete(secondary)
            db.flush()

        if member_user is None:
            member_user = User(
                auth_user_id=auth_user_id,
                email=args.member_email,
                full_name="Linka QA Member",
                is_active=True,
            )
            db.add(member_user)
            db.flush()
        else:
            member_user.auth_user_id = auth_user_id
            member_user.full_name = "Linka QA Member"
            member_user.is_active = True
            db.flush()

        db.add(WorkspaceMember(
            workspace_id=workspace_id,
            user_id=member_user.id,
            role="member",
            is_active=True,
        ))

        _seed_primary(db, workspace, ids)
        secondary = _seed_secondary(
            db,
            primary_workspace_id=workspace_id,
            member_user=member_user,
            ids=ids,
        )
        db.commit()

        print(f"Primary workspace: {workspace.id}")
        print(f"Secondary workspace: {secondary.id}")
        print(f"Member user: {member_user.id}")
        print("Final internal gate fixtures seeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
