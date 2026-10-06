from __future__ import annotations

import os
import threading
import time as wall_time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.automation_worker import AutomationWorker
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.channel_connection import ChannelConnection
from app.models.clinic_integration import ClinicIntegration
from app.models.clinic_inventory import InventoryItem
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.agent_knowledge import build_agent_knowledge_snapshot
from app.services.booking import calculate_availability
from app.services.demo_reset import (
    CLEAR_TABLES,
    DEMO_RESET_ACTION,
    PRESERVE_TABLES,
    RESET_RESEED_TABLES,
    DemoResetError,
    DemoResetInProgress,
    _acquire_reset_lock,
    _workspace_tables,
    acquire_demo_request_lock,
    capture_demo_canonical_seed,
    reset_demo_workspace,
    scheduled_demo_reset_state,
    validate_demo_reset_manifest,
)
from app.services.workspace_runtime_policy import workspace_runtime_policy


def _engine():
    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"localhost", "127.0.0.1", "::1", "postgres"} or url.database != "ci_db":
        pytest.fail("Demo reset PostgreSQL gate requires disposable ci_db.")
    return create_engine(url, connect_args={"connect_timeout": 3})


def _fixture(db: Session, *, demo: bool = True, slug_prefix: str = "demo"):
    suffix = uuid4().hex[:10]
    user = User(email=f"{slug_prefix}-{suffix}@example.test", auth_user_id=uuid4())
    workspace = Workspace(
        name=f"{slug_prefix.title()} Clinic",
        slug=f"{slug_prefix}-{suffix}",
        timezone="Africa/Cairo",
        is_demo=demo,
        is_active=True,
    )
    db.add_all([user, workspace])
    db.flush()
    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role="admin",
        is_active=True,
    )
    branch = Branch(
        workspace_id=workspace.id,
        name="Zayed",
        code=f"zayed-{suffix}",
        city="Giza",
        timezone="Africa/Cairo",
    )
    service = Service(
        workspace_id=workspace.id,
        name="Hydrafacial",
        slug=f"hydrafacial-{suffix}",
        category="facial",
        duration_minutes=30,
        price_minor=100_000,
        currency="EGP",
    )
    staff = Staff(
        workspace_id=workspace.id,
        first_name="Mariam",
        last_name="Hassan",
        email=f"doctor-{suffix}@example.test",
        job_title="Dermatologist",
    )
    db.add_all([member, branch, service, staff])
    db.flush()
    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        specialization="Dermatology",
        booking_enabled=True,
    )
    db.add(doctor)
    db.flush()
    assignment = DoctorBranch(
        workspace_id=workspace.id,
        doctor_id=doctor.id,
        branch_id=branch.id,
        is_primary=True,
    )
    doctor_service = DoctorService(
        workspace_id=workspace.id,
        doctor_id=doctor.id,
        service_id=service.id,
    )
    branch_hours = BranchWorkingHour(
        workspace_id=workspace.id,
        branch_id=branch.id,
        weekday=0,
        start_time=time(9, 0),
        end_time=time(18, 0),
    )
    doctor_hours = DoctorWorkingHour(
        workspace_id=workspace.id,
        doctor_id=doctor.id,
        branch_id=branch.id,
        weekday=0,
        start_time=time(9, 0),
        end_time=time(12, 0),
    )
    booking = BookingSettings(
        workspace_id=workspace.id,
        slot_interval_minutes=30,
        minimum_notice_minutes=0,
        booking_horizon_days=90,
        cancellation_notice_minutes=60,
        allow_same_day_booking=True,
        require_confirmation=False,
    )
    patient = Patient(
        workspace_id=workspace.id,
        first_name="Nour",
        last_name="Test",
        phone=f"+2010{suffix[:8]}",
        phone_normalized=f"2010{suffix[:8]}",
        preferred_branch_id=branch.id,
        source="other",
    )
    inventory = InventoryItem(
        workspace_id=workspace.id,
        name=f"Botox-{suffix}",
        category="injectable",
        quantity_ml=Decimal("20"),
        concentration_mg_per_ml=Decimal("10"),
        low_stock_threshold_ml=Decimal("5"),
    )
    integration = ClinicIntegration(
        workspace_id=workspace.id,
        mode="tia_native",
        adapter_key="tia_database",
        status="active",
        config_json={"ui_demo_config": True},
    )
    connection = ChannelConnection(
        workspace_id=workspace.id,
        channel="whatsapp",
        provider="meta_cloud",
        display_name="Demo WhatsApp",
        status="paused",
        external_account_id=None,
        adapter_token_hash=(suffix * 7)[:64],
        config_json={"mock": True, "transport_ready": False},
    )
    worker = AutomationWorker(
        workspace_id=workspace.id,
        name="Linka Railway Automation Scheduler",
        token_hash=uuid4().hex + uuid4().hex,
        status="active",
        created_by_user_id=None,
    )
    db.add_all([
        assignment,
        doctor_service,
        branch_hours,
        doctor_hours,
        booking,
        patient,
        inventory,
        integration,
        connection,
        worker,
    ])
    db.flush()
    workspace.primary_branch_id = branch.id

    start_at = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    appointment = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch.id,
        doctor_id=doctor.id,
        service_id=service.id,
        status="completed",
        source="staff",
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        busy_start_at=start_at,
        busy_end_at=start_at + timedelta(minutes=30),
        duration_minutes=30,
        price_minor=100_000,
        currency="EGP",
        completed_at=start_at + timedelta(minutes=30),
    )
    package = PatientPackage(
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service.id,
        name="Hydrafacial 4",
        sessions_purchased=4,
        sale_price_minor=320_000,
        currency="EGP",
        purchased_at=datetime(2026, 9, 1, tzinfo=UTC),
        source="staff",
    )
    db.add_all([appointment, package])
    db.flush()
    payment = PaymentTransaction(
        workspace_id=workspace.id,
        appointment_id=appointment.id,
        origin_appointment_id=appointment.id,
        patient_id=patient.id,
        patient_package_id=package.id,
        transaction_type="payment",
        amount_minor=320_000,
        currency="EGP",
        payment_method="cash",
        source="staff",
    )
    db.add(payment)
    db.flush()
    package.purchase_transaction_id = payment.id
    appointment.patient_package_id = package.id
    usage = PackageUsage(
        workspace_id=workspace.id,
        patient_package_id=package.id,
        appointment_id=appointment.id,
        sessions_used=1,
        status="consumed",
        used_at=appointment.completed_at,
    )
    db.add(usage)
    db.commit()
    return {
        "user": user,
        "workspace": workspace,
        "member": member,
        "branch": branch,
        "service": service,
        "doctor": doctor,
        "patient": patient,
        "appointment": appointment,
        "package": package,
        "inventory": inventory,
        "integration": integration,
        "connection": connection,
        "worker": worker,
    }


def _monday_after(days: int = 14) -> date:
    current = datetime.now(UTC).date() + timedelta(days=days)
    return current + timedelta(days=(7 - current.weekday()) % 7)


def _business_signature(db: Session, workspace_id) -> tuple:
    service = db.scalar(select(Service).where(Service.workspace_id == workspace_id))
    hours = list(
        db.scalars(
            select(DoctorWorkingHour)
            .where(DoctorWorkingHour.workspace_id == workspace_id)
            .order_by(DoctorWorkingHour.weekday, DoctorWorkingHour.start_time)
        )
    )
    return (
        service.name,
        service.price_minor,
        tuple((row.weekday, row.start_time.isoformat(), row.end_time.isoformat()) for row in hours),
        db.scalar(select(func.count()).select_from(Patient).where(Patient.workspace_id == workspace_id)),
        db.scalar(select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace_id)),
        db.scalar(select(func.count()).select_from(PatientPackage).where(PatientPackage.workspace_id == workspace_id)),
        str(db.scalar(select(InventoryItem.quantity_ml).where(InventoryItem.workspace_id == workspace_id))),
    )


def test_demo_reset_manifest_classifies_every_workspace_table() -> None:
    validate_demo_reset_manifest()
    discovered = set(_workspace_tables())
    assert len(discovered) == 76
    assert discovered == set(PRESERVE_TABLES | RESET_RESEED_TABLES | CLEAR_TABLES)


def test_demo_mutations_are_visible_then_reset_restores_canonical_state() -> None:
    engine = _engine()
    with Session(engine) as db:
        fx = _fixture(db)
        wid = fx["workspace"].id
        member_id = fx["member"].id
        integration_config = dict(fx["integration"].config_json)
        connection_id = fx["connection"].id
        worker_id = fx["worker"].id
        canonical = _business_signature(db, wid)
        capture_demo_canonical_seed(db, workspace_id=wid)

        service = db.scalar(select(Service).where(Service.workspace_id == wid))
        service.price_minor = 777_000
        hours = db.scalar(select(DoctorWorkingHour).where(DoctorWorkingHour.workspace_id == wid))
        hours.start_time = time(15, 0)
        hours.end_time = time(17, 0)
        inventory = db.scalar(select(InventoryItem).where(InventoryItem.workspace_id == wid))
        inventory.quantity_ml = Decimal("3")
        extra_patient = Patient(
            workspace_id=wid,
            first_name="Temporary",
            phone=f"+2011{uuid4().hex[:8]}",
            phone_normalized=f"2011{uuid4().hex[:8]}",
            source="other",
        )
        db.add(extra_patient)
        db.flush()
        canonical_patient = db.scalar(select(Patient).where(Patient.workspace_id == wid, Patient.first_name == "Nour"))
        branch = db.scalar(select(Branch).where(Branch.workspace_id == wid))
        doctor = db.scalar(select(Doctor).where(Doctor.workspace_id == wid))
        extra_appointment = Appointment(
            workspace_id=wid,
            patient_id=extra_patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            service_id=service.id,
            status="cancelled",
            source="staff",
            start_at=datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
            end_at=datetime(2026, 10, 5, 14, 30, tzinfo=UTC),
            busy_start_at=datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
            busy_end_at=datetime(2026, 10, 5, 14, 30, tzinfo=UTC),
            duration_minutes=30,
            price_minor=777_000,
            currency="EGP",
            cancellation_reason="temporary demo mutation",
        )
        extra_package = PatientPackage(
            workspace_id=wid,
            patient_id=canonical_patient.id,
            service_id=service.id,
            name="Temporary Package",
            sessions_purchased=2,
            sale_price_minor=100_000,
            currency="EGP",
            purchased_at=datetime.now(UTC),
            source="staff",
        )
        db.add_all([extra_appointment, extra_package])
        db.commit()

        knowledge = build_agent_knowledge_snapshot(db, db.get(Workspace, wid))
        assert next(item for item in knowledge.services if item.slug == service.slug).price_minor == 777_000

        booking_date = _monday_after()
        _tz, slots = calculate_availability(
            db=db,
            workspace=db.get(Workspace, wid),
            branch_id=branch.id,
            service_id=service.id,
            booking_date=booking_date,
            doctor_id=doctor.id,
        )
        assert slots
        assert min(slot.start_at.astimezone(ZoneInfo("Africa/Cairo")).hour for slot in slots) >= 15

        reset_demo_workspace(db, workspace_id=wid, reset_date=date(2026, 9, 17))
        assert _business_signature(db, wid) == canonical
        assert db.get(WorkspaceMember, member_id) is not None
        assert db.get(ClinicIntegration, wid).config_json == integration_config
        assert db.get(ChannelConnection, connection_id) is not None
        assert db.get(AutomationWorker, worker_id) is not None
        assert db.scalar(select(Patient).where(Patient.workspace_id == wid, Patient.first_name == "Temporary")) is None
        assert db.scalar(select(PatientPackage).where(PatientPackage.workspace_id == wid, PatientPackage.name == "Temporary Package")) is None

        first = _business_signature(db, wid)
        reset_demo_workspace(db, workspace_id=wid, reset_date=date(2026, 9, 17))
        assert _business_signature(db, wid) == first
        db.execute(delete(Workspace).where(Workspace.id == wid))
        db.execute(delete(User).where(User.id == fx["user"].id))
        db.commit()
    engine.dispose()


def test_demo_reset_injected_failure_rolls_back_and_writes_no_success_marker() -> None:
    engine = _engine()
    with Session(engine) as db:
        fx = _fixture(db)
        wid = fx["workspace"].id
        capture_demo_canonical_seed(db, workspace_id=wid)
        service = db.scalar(select(Service).where(Service.workspace_id == wid))
        service.price_minor = 555_000
        db.commit()
        before = _business_signature(db, wid)
        with pytest.raises(DemoResetError, match="Injected"):
            reset_demo_workspace(db, workspace_id=wid, injected_failure_after_clear=True)
        assert _business_signature(db, wid) == before
        markers = db.scalar(
            select(func.count()).select_from(__import__("app.models.activity_event", fromlist=["ActivityEvent"]).ActivityEvent).where(
                __import__("app.models.activity_event", fromlist=["ActivityEvent"]).ActivityEvent.workspace_id == wid,
                __import__("app.models.activity_event", fromlist=["ActivityEvent"]).ActivityEvent.action == DEMO_RESET_ACTION,
            )
        )
        assert markers == 0
        db.execute(delete(Workspace).where(Workspace.id == wid))
        db.execute(delete(User).where(User.id == fx["user"].id))
        db.commit()
    engine.dispose()


def test_daily_marker_restart_and_missed_window_behavior() -> None:
    engine = _engine()
    with Session(engine) as db:
        fx = _fixture(db)
        wid = fx["workspace"].id
        capture_demo_canonical_seed(db, workspace_id=wid)
        workspace = db.get(Workspace, wid)
        in_window = datetime(2026, 9, 17, 1, 5, tzinfo=UTC)  # 04:05 Cairo
        assert scheduled_demo_reset_state(db, workspace=workspace, now=in_window) == "reset"
    with Session(engine) as restarted:
        workspace = restarted.get(Workspace, wid)
        assert scheduled_demo_reset_state(restarted, workspace=workspace, now=in_window) == "already_reset"
        service = restarted.scalar(select(Service).where(Service.workspace_id == wid))
        service.price_minor = 999_000
        restarted.commit()
        midday = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
        assert scheduled_demo_reset_state(restarted, workspace=workspace, now=midday) == "missed_window"
        assert (
            scheduled_demo_reset_state(restarted, workspace=workspace, now=midday)
            == "missed_window_recorded"
        )
        assert restarted.scalar(select(Service.price_minor).where(Service.workspace_id == wid)) == 999_000
        user_id = restarted.scalar(select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == wid))
        restarted.execute(delete(Workspace).where(Workspace.id == wid))
        restarted.execute(delete(User).where(User.id == user_id))
        restarted.commit()
    engine.dispose()


def test_real_workspace_isolation_and_manual_reset_guard() -> None:
    engine = _engine()
    with Session(engine) as db:
        demo = _fixture(db, demo=True, slug_prefix="demo-isolation")
        real = _fixture(db, demo=False, slug_prefix="real-isolation")
        capture_demo_canonical_seed(db, workspace_id=demo["workspace"].id)
        real_before = _business_signature(db, real["workspace"].id)
        reset_demo_workspace(db, workspace_id=demo["workspace"].id)
        assert _business_signature(db, real["workspace"].id) == real_before
        with pytest.raises(DemoResetError, match="real workspace"):
            reset_demo_workspace(db, workspace_id=real["workspace"].id)
        for fx in (demo, real):
            db.execute(delete(Workspace).where(Workspace.id == fx["workspace"].id))
            db.execute(delete(User).where(User.id == fx["user"].id))
        db.commit()
    engine.dispose()


def test_demo_runtime_policy_blocks_all_external_capabilities() -> None:
    demo = Workspace(name="Demo", slug=f"demo-policy-{uuid4().hex[:8]}", is_demo=True)
    real = Workspace(name="Real", slug=f"real-policy-{uuid4().hex[:8]}", is_demo=False)
    demo_policy = workspace_runtime_policy(demo)
    assert demo_policy.allow_external_dispatch is False
    assert demo_policy.allow_external_configuration is False
    assert demo_policy.allow_external_ingress is False
    assert demo_policy.allow_external_sync is False
    real_policy = workspace_runtime_policy(real)
    assert real_policy.allow_external_dispatch is True
    assert real_policy.allow_external_configuration is True
    assert real_policy.allow_external_ingress is True
    assert real_policy.allow_external_sync is True


def test_reset_waits_for_active_demo_request_shared_lock() -> None:
    engine = _engine()
    with Session(engine) as setup:
        fx = _fixture(setup, demo=True, slug_prefix="lock-wait")
        wid = fx["workspace"].id
        uid = fx["user"].id
        capture_demo_canonical_seed(setup, workspace_id=wid)

    request_ready = threading.Event()
    release_request = threading.Event()

    def active_request():
        with Session(engine) as db:
            workspace = db.get(Workspace, wid)
            acquire_demo_request_lock(db, workspace)
            request_ready.set()
            assert release_request.wait(5)
            db.commit()

    def reset_request():
        with Session(engine) as db:
            return reset_demo_workspace(db, workspace_id=wid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        active_future = pool.submit(active_request)
        assert request_ready.wait(3)
        reset_future = pool.submit(reset_request)
        wall_time.sleep(0.25)
        assert not reset_future.done()
        release_request.set()
        active_future.result(timeout=3)
        assert reset_future.result(timeout=5).workspace_id == wid

    with Session(engine) as cleanup:
        cleanup.execute(delete(Workspace).where(Workspace.id == wid))
        cleanup.execute(delete(User).where(User.id == uid))
        cleanup.commit()
    engine.dispose()


def test_new_demo_request_fails_safely_while_reset_exclusive_lock_is_held() -> None:
    engine = _engine()
    with Session(engine) as setup:
        fx = _fixture(setup, demo=True, slug_prefix="lock-exclusive")
        wid = fx["workspace"].id
        uid = fx["user"].id
    with Session(engine) as reset_session:
        _acquire_reset_lock(reset_session, wid)
        with Session(engine) as request_session:
            workspace = request_session.get(Workspace, wid)
            started = wall_time.monotonic()
            with pytest.raises(DemoResetInProgress):
                acquire_demo_request_lock(request_session, workspace)
            assert wall_time.monotonic() - started >= 2.5
        reset_session.rollback()
    with Session(engine) as cleanup:
        cleanup.execute(delete(Workspace).where(Workspace.id == wid))
        cleanup.execute(delete(User).where(User.id == uid))
        cleanup.commit()
    engine.dispose()
