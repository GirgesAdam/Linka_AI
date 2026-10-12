from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.agents.v2.turn_interpreter as turn_interpreter
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.clinic_integration import ClinicIntegration
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.services.agent_v2.orchestrator import V2OrchestratedTurn, orchestrate_v2_turn
from app.services.agent_v2.state import BookingTaskState

CAIRO = ZoneInfo("Africa/Cairo")
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=CAIRO)
ORIGINAL = (
    "عايزة أحجز ليزر إبط وليزر بكيني مع بعض في نفس الزيارة، "
    "اختار دكتور واحد يعمل الاتنين ووريني أقرب ميعاد مناسب قبل ما تحجز"
)
TOTAL = "السعر الإجمالي كام؟"
MODE = os.environ.get("RA_G_MODE", "standalone").strip().lower()


@contextmanager
def rollback_session():
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield db
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
        engine.dispose()


def _device_price(
    db: Session,
    *,
    workspace: Workspace,
    service: Service,
    device_key: str,
    device_name: str,
    price_minor: int,
    duration_minutes: int,
) -> None:
    if db.scalar(
        select(ClinicLaserDevice).where(
            ClinicLaserDevice.workspace_id == workspace.id,
            ClinicLaserDevice.device_key == device_key,
        )
    ) is None:
        db.add(
            ClinicLaserDevice(
                workspace_id=workspace.id,
                device_key=device_key,
                name=device_name,
                is_active=True,
            )
        )
        db.flush()
    db.add(
        ServiceDevicePrice(
            workspace_id=workspace.id,
            service_id=service.id,
            device_key=device_key,
            device_name=device_name,
            price_minor=price_minor,
            duration_minutes=duration_minutes,
            currency="EGP",
            is_active=True,
        )
    )
    db.flush()


def build_fixture(db: Session):
    suffix = uuid4().hex[:10]
    workspace = Workspace(
        name="Linka RA-G Clinic",
        slug=f"rag-{suffix}",
        timezone="Africa/Cairo",
        is_active=True,
        is_demo=True,
    )
    db.add(workspace)
    db.flush()
    branch = Branch(
        workspace_id=workspace.id,
        name="فرع مدينة نصر",
        code=f"RAG-{suffix}",
        city="Cairo",
        country_code="EG",
        timezone="Africa/Cairo",
        is_active=True,
    )
    staff = Staff(
        workspace_id=workspace.id,
        first_name="مريم",
        last_name="أحمد",
        is_active=True,
    )
    underarm = Service(
        workspace_id=workspace.id,
        name="ليزر إبط",
        slug=f"laser-underarm-{suffix}",
        operational_category="laser",
        duration_minutes=20,
        price_minor=50_000,
        currency="EGP",
        requires_laser_device=True,
        is_active=True,
    )
    bikini = Service(
        workspace_id=workspace.id,
        name="ليزر بكيني",
        slug=f"laser-bikini-{suffix}",
        operational_category="laser",
        duration_minutes=30,
        price_minor=70_000,
        currency="EGP",
        requires_laser_device=True,
        is_active=True,
    )
    arms = Service(
        workspace_id=workspace.id,
        name="Full Arms",
        slug=f"full-arms-{suffix}",
        operational_category="laser",
        duration_minutes=40,
        price_minor=90_000,
        currency="EGP",
        requires_laser_device=True,
        is_active=True,
    )
    patient = Patient(
        workspace_id=workspace.id,
        first_name="سارة",
        last_name="تجربة",
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add_all([branch, staff, underarm, bikini, arms, patient])
    db.flush()
    workspace.primary_branch_id = branch.id
    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        specialization="Dermatology",
        booking_enabled=True,
        is_active=True,
    )
    db.add(doctor)
    db.flush()
    db.add(
        DoctorBranch(
            workspace_id=workspace.id,
            doctor_id=doctor.id,
            branch_id=branch.id,
            is_primary=True,
            is_active=True,
        )
    )
    for service in (underarm, bikini, arms):
        db.add(
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                service_id=service.id,
                is_active=True,
            )
        )
    for weekday in range(7):
        db.add(
            BranchWorkingHour(
                workspace_id=workspace.id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=time(10),
                end_time=time(22),
            )
        )
        db.add(
            DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=doctor.id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=time(10),
                end_time=time(22),
            )
        )
    db.add(
        BookingSettings(
            workspace_id=workspace.id,
            slot_interval_minutes=30,
            minimum_notice_minutes=0,
            booking_horizon_days=30,
            cancellation_notice_minutes=60,
            allow_same_day_booking=True,
            require_confirmation=True,
        )
    )
    db.add(
        ClinicIntegration(
            workspace_id=workspace.id,
            mode="tia_native",
            adapter_key="tia_database",
            status="active",
            config_json={},
            authority_policy_json={},
        )
    )
    db.flush()
    for service, price, duration in (
        (underarm, 50_000, 20),
        (bikini, 70_000, 30),
        (arms, 90_000, 40),
    ):
        _device_price(
            db,
            workspace=workspace,
            service=service,
            device_key="candela_gentle",
            device_name="Candela Gentle",
            price_minor=price,
            duration_minutes=duration,
        )
    db.flush()
    return workspace, patient, doctor, underarm, bikini, arms


def dump(value):
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


RAW: list[dict[str, object]] = []
_original_invoke = turn_interpreter.invoke_typed_structured_output


def capture_invoke(*args, **kwargs):
    model = kwargs.get("model") if "model" in kwargs else (args[0] if args else None)
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__
    try:
        value = _original_invoke(*args, **kwargs)
    except Exception as exc:
        RAW.append(
            {
                "status": "error",
                "model": str(model_name),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        raise
    RAW.append({"status": "ok", "model": str(model_name), "structured": dump(value)})
    return value


turn_interpreter.invoke_typed_structured_output = capture_invoke


def appointment_count(db: Session, workspace_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace_id)
        )
        or 0
    )


def turn_record(turn: V2OrchestratedTurn, *, customer: str, raw: list[dict[str, object]]) -> dict[str, object]:
    task = turn.active_task
    return {
        "customer": customer,
        "raw_model_structured": raw,
        "understanding": dump(turn.understanding),
        "plan": dump(turn.plan),
        "traces": [dump(trace) for trace in turn.traces],
        "outcomes": [dump(outcome) for outcome in turn.outcomes],
        "active_task": dump(task),
        "requested_service_ids": list(task.requested_service_ids) if isinstance(task, BookingTaskState) else [],
        "device_key": task.constraints.device_key if isinstance(task, BookingTaskState) else None,
        "doctor_id": task.constraints.doctor_id if isinstance(task, BookingTaskState) else None,
        "proposal": dump(task.proposal) if isinstance(task, BookingTaskState) else None,
        "pending_write": dump(turn.pending_write),
        "reply": turn.reply,
        "responder_model": turn.responder_model,
    }


def run_scenario(name: str, messages: list[str], validator) -> None:
    with rollback_session() as db:
        workspace, patient, doctor, underarm, bikini, arms = build_fixture(db)
        ids = {
            "underarm": str(underarm.id),
            "bikini": str(bikini.id),
            "arms": str(arms.id),
            "doctor": str(doctor.id),
        }
        conversation_id = uuid4()
        history = []
        before = appointment_count(db, workspace.id)
        records: list[dict[str, object]] = []
        for index, customer in enumerate(messages, start=1):
            history.append(HumanMessage(content=customer))
            RAW.clear()
            result = orchestrate_v2_turn(
                db=db,
                workspace=workspace,
                patient=patient,
                conversation_id=conversation_id,
                run_id=uuid4(),
                history=history,
                local_now=NOW,
                timezone_name=workspace.timezone,
                clinic_name=workspace.name,
                turn_id=f"{name.lower()}-{index}",
                write_executor=None,
            )
            records.append(turn_record(result, customer=customer, raw=list(RAW)))
            if result.reply:
                history.append(AIMessage(content=result.reply))
        after = appointment_count(db, workspace.id)
        payload = {
            "scenario": name,
            "mode": MODE,
            "ids": ids,
            "appointment_count_before": before,
            "appointment_count_after": after,
            "turns": records,
        }
        print("RA_G_RESULT=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
        assert before == 0 and after == 0, f"{name}: audit must not create appointments"
        validator(records, ids)


def _task_ids(record: dict[str, object]) -> list[str]:
    return list(record.get("requested_service_ids") or [])


def validate_g1(records, ids):
    first = records[-1]
    assert set(_task_ids(first)) == {ids["underarm"], ids["bikini"]}
    assert "أنهي خدمة" not in str(first.get("reply") or "")
    assert first["pending_write"] is None


def validate_g2(records, ids):
    assert set(_task_ids(records[0])) == {ids["underarm"], ids["bikini"]}
    assert set(_task_ids(records[-1])) == {ids["underarm"], ids["bikini"]}
    assert "أنهي خدمة" not in str(records[-1].get("reply") or "")


def validate_g3(records, ids):
    expected = {ids["underarm"], ids["bikini"]}
    for record in records:
        assert set(_task_ids(record)) == expected
        assert "أنهي خدمة" not in str(record.get("reply") or "")


def validate_g4(records, ids):
    assert set(_task_ids(records[0])) == {ids["underarm"], ids["bikini"]}
    assert _task_ids(records[1]) == [ids["underarm"]]
    assert _task_ids(records[2]) == [ids["underarm"]]
    assert "أنهي خدمة" not in str(records[2].get("reply") or "")


def validate_g5(records, ids):
    expected = {ids["underarm"], ids["bikini"]}
    assert set(_task_ids(records[0])) == expected
    final = records[-1]
    assert set(_task_ids(final)) == expected
    proposal = final.get("proposal")
    assert isinstance(proposal, dict), "RA-G5 must create a proposal before any clinic write"
    assert set(proposal.get("service_ids") or []) == expected
    assert len(proposal.get("additional_service_ids") or []) == 1
    assert final["pending_write"] is None


def main() -> int:
    if MODE not in {"standalone", "batch1-overlay"}:
        raise RuntimeError(f"Unsupported RA_G_MODE={MODE}")

    run_scenario("RA-G1", [ORIGINAL], validate_g1)
    if MODE == "batch1-overlay":
        run_scenario("RA-G2", [ORIGINAL, TOTAL], validate_g2)
        run_scenario(
            "RA-G3",
            [
                ORIGINAL,
                "مين الدكاترة اللي بيعملوا الخدمتين دول؟",
                TOTAL,
                "السبت الساعة 6 مساء مناسب",
            ],
            validate_g3,
        )
        run_scenario("RA-G4", [ORIGINAL, "خليها ليزر إبط بس", TOTAL], validate_g4)
    run_scenario(
        "RA-G5",
        [ORIGINAL, "السبت الساعة 6 مساء مناسب، احجزيه ووريني التفاصيل قبل التأكيد النهائي"],
        validate_g5,
    )
    print(f"RA_G_SUMMARY={{\"mode\":\"{MODE}\",\"status\":\"PASS\"}}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
