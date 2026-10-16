from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

if __package__:
    from .grouped_multiservice_intent_review_contract import validate_g6_visit_time_selection
else:
    from grouped_multiservice_intent_review_contract import validate_g6_visit_time_selection
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.agents.v2.turn_interpreter as turn_interpreter
import app.services.agent_v2.orchestrator as orchestrator_module
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.clinic_integration import ClinicIntegration
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace
from app.services.agent_v2.live_chat import (
    _availability_reference_context_from_turn,
    _verified_read_context_from_turn,
)
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
C7_INITIAL = "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0648\u0625\u0628\u0637 \u0645\u0639 \u0628\u0639\u0636 \u0639\u0644\u0649 Candela Gentle \u064a\u0648\u0645 \u0627\u0644\u0633\u0628\u062a 17 \u0623\u0643\u062a\u0648\u0628\u0631"
C7_REPLACEMENT = "\u0628\u062f\u0644 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u062e\u0644\u064a\u0647\u0627 Full Arms \u0648\u062e\u0644\u064a \u0644\u064a\u0632\u0631 \u0627\u0644\u0625\u0628\u0637 \u0632\u064a \u0645\u0627 \u0647\u0648"
C7_REPLACEMENT_VARIANT_1 = "\u0634\u064a\u0644 \u0627\u0644\u0628\u0643\u064a\u0646\u064a \u0648\u062d\u0637 \u0645\u0643\u0627\u0646\u0647 Full Arms \u0648\u0633\u064a\u0628 \u0627\u0644\u0625\u0628\u0637"
C7_REPLACEMENT_VARIANT_2 = "\u062e\u0644\u064a \u0627\u0644\u0625\u0628\u0637 \u0648Full Arms \u0628\u0633"
C7_ADDITION = "\u0636\u064a\u0641 Full Arms \u0643\u0645\u0627\u0646"
C7_REMOVAL = "\u0634\u064a\u0644 \u0627\u0644\u0628\u0643\u064a\u0646\u064a \u0648\u062e\u0644\u064a \u0627\u0644\u0625\u0628\u0637 \u0628\u0633"
RA_G6_INITIAL = (
    "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a "
    "\u0645\u0639 \u0628\u0639\u0636 \u0641\u064a \u0646\u0641\u0633 \u0627\u0644\u0632\u064a\u0627\u0631\u0629 \u064a\u0648\u0645 \u0627\u0644\u0633\u0628\u062a 17 \u0623\u0643\u062a\u0648\u0628\u0631\u060c "
    "\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u0645\u0648\u0627\u0639\u064a\u062f \u0627\u0644\u0645\u062a\u0627\u062d\u0629 \u0642\u0628\u0644 \u0645\u0627 \u062a\u062d\u062c\u0632"
)
RA_G6_PRICE = "\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a \u0643\u0627\u0645\u061f"
RA_G6_TIME = "\u0627\u0644\u0633\u0627\u0639\u0629 8 \u0645\u0633\u0627\u0621 \u0645\u0646\u0627\u0633\u0628\u0629"
RA_G7_INITIAL = (
    "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0625\u0628\u0637 \u0648\u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0648\u0643\u0634\u0641 \u062c\u0644\u062f\u064a\u0629 \u064a\u0648\u0645 \u0627\u0644\u0633\u0628\u062a 17 \u0623\u0643\u062a\u0648\u0628\u0631\u060c "
    "\u062e\u0644\u064a\u0647\u0645 \u0648\u0631\u0627 \u0628\u0639\u0636 \u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u0645\u0648\u0627\u0639\u064a\u062f \u0648\u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a"
)
RA_G7_BOOK = (
    "\u0627\u0644\u0633\u0628\u062a \u0627\u0644\u0633\u0627\u0639\u0629 6 \u0645\u0633\u0627\u0621 \u0645\u0646\u0627\u0633\u0628\u060c \u0627\u062d\u062c\u0632\u064a \u0627\u0644\u062e\u062f\u0645\u0627\u062a \u0627\u0644\u062b\u0644\u0627\u062b\u0629 \u0648\u0631\u0627 \u0628\u0639\u0636 "
    "\u0648\u0648\u0631\u064a\u0646\u064a \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a"
)
RA_G7_CONFIRM_DETAILS = (
    "\u062a\u0645\u0627\u0645\u060c \u0643\u0645\u0644\u064a \u0646\u0641\u0633 \u0627\u0644\u062d\u062c\u0632 \u064a\u0648\u0645 \u0627\u0644\u0633\u0628\u062a \u0627\u0644\u0633\u0627\u0639\u0629 6 \u0645\u0633\u0627\u0621 \u0648\u0648\u0631\u064a\u0646\u064a \u0643\u0644 \u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0642\u0628\u0644 \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a"
)


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
    dermatology_staff = Staff(
        workspace_id=workspace.id,
        first_name="Dermatology",
        last_name="Doctor",
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
    dermatology = Service(
        workspace_id=workspace.id,
        name="\u0643\u0634\u0641 \u062c\u0644\u062f\u064a\u0629",
        slug=f"dermatology-consultation-{suffix}",
        operational_category="dermatology",
        duration_minutes=30,
        price_minor=100_000,
        currency="EGP",
        requires_laser_device=False,
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
    db.add_all([branch, staff, dermatology_staff, underarm, bikini, arms, dermatology, patient])
    db.flush()
    workspace.primary_branch_id = branch.id
    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        specialization="Dermatology",
        booking_enabled=True,
        is_active=True,
    )
    dermatology_doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=dermatology_staff.id,
        specialization="Dermatology",
        booking_enabled=True,
        is_active=True,
    )
    db.add_all([doctor, dermatology_doctor])
    db.flush()
    for assigned_doctor in (doctor, dermatology_doctor):
        db.add(
            DoctorBranch(
                workspace_id=workspace.id,
                doctor_id=assigned_doctor.id,
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
    db.add(
        DoctorService(
            workspace_id=workspace.id,
            doctor_id=dermatology_doctor.id,
            service_id=dermatology.id,
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
    for weekday in range(7):
        db.add(
            DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=dermatology_doctor.id,
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
    return workspace, patient, doctor, dermatology_doctor, underarm, bikini, arms, dermatology


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


STAGES: list[dict[str, object]] = []
_original_collapse = orchestrator_module.collapse_booking_service_sets
_original_expand = orchestrator_module.expand_multi_service_operations


def capture_collapse(*args, **kwargs):
    before = args[0] if args else kwargs.get("turn")
    value = _original_collapse(*args, **kwargs)
    understanding, service_sets = value
    STAGES.append({
        "stage": "collapse_booking_service_sets",
        "input_understanding": dump(before),
        "output_understanding": dump(understanding),
        "canonical_service_sets": service_sets,
    })
    return value


def capture_expand(*args, **kwargs):
    before = args[0] if args else kwargs.get("turn")
    value = _original_expand(*args, **kwargs)
    understanding, visit_groups = value
    STAGES.append({
        "stage": "expand_multi_service_operations",
        "input_understanding": dump(before),
        "output_understanding": dump(understanding),
        "visit_groups": visit_groups,
    })
    return value


orchestrator_module.collapse_booking_service_sets = capture_collapse
orchestrator_module.expand_multi_service_operations = capture_expand


def appointment_count(db: Session, workspace_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace_id)
        )
        or 0
    )


def turn_record(
    turn: V2OrchestratedTurn,
    *,
    customer: str,
    raw: list[dict[str, object]],
    stages: list[dict[str, object]],
    active_task_before,
    availability_reference_context: dict[str, object] | None = None,
) -> dict[str, object]:
    task = turn.active_task
    return {
        "customer": customer,
        "raw_model_structured": raw,
        "normalization_stages": stages,
        "understanding": dump(turn.understanding),
        "active_task_before": dump(active_task_before),
        "plan": dump(turn.plan),
        "traces": [dump(trace) for trace in turn.traces],
        "outcomes": [dump(outcome) for outcome in turn.outcomes],
        "active_task": dump(task),
        "requested_service_ids": list(task.requested_service_ids) if isinstance(task, BookingTaskState) else [],
        "device_key": task.constraints.device_key if isinstance(task, BookingTaskState) else None,
        "doctor_id": task.constraints.doctor_id if isinstance(task, BookingTaskState) else None,
        "proposal": dump(task.proposal) if isinstance(task, BookingTaskState) else None,
        "pending_write": dump(turn.pending_write),
        "availability_reference_context": availability_reference_context,
        "reply": turn.reply,
        "responder_model": turn.responder_model,
        "reference_action": turn.reference_action,
        "selected_option_ref": turn.selected_option_ref,
        "full_interpreter_called": turn.full_interpreter_called,
    }


def run_scenario(name: str, messages: list[str], validator) -> None:
    selected = {
        item.strip()
        for item in os.environ.get("RA_G_SCENARIOS", "").split(",")
        if item.strip()
    }
    if selected and name not in selected:
        return
    with rollback_session() as db:
        (
            workspace, patient, doctor, dermatology_doctor,
            underarm, bikini, arms, dermatology,
        ) = build_fixture(db)
        ids = {
            "underarm": str(underarm.id),
            "bikini": str(bikini.id),
            "arms": str(arms.id),
            "dermatology": str(dermatology.id),
            "doctor": str(doctor.id),
            "dermatology_doctor": str(dermatology_doctor.id),
        }
        conversation_id = uuid4()
        db.add(
            Conversation(
                id=conversation_id,
                workspace_id=workspace.id,
                patient_id=patient.id,
                channel="web",
                status="open",
                owner_type="ai",
                unread_count=0,
                started_at=NOW,
                last_message_at=NOW,
            )
        )
        db.flush()
        history = []
        before = appointment_count(db, workspace.id)
        records: list[dict[str, object]] = []
        previous_task = None
        recent_read_context: dict[str, object] | None = None
        availability_reference_context: dict[str, object] | None = None
        for index, customer in enumerate(messages, start=1):
            history.append(HumanMessage(content=customer))
            RAW.clear()
            STAGES.clear()
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
                recent_read_context=recent_read_context,
                availability_reference_context=availability_reference_context,
            )
            recent_read_context = _verified_read_context_from_turn(
                db,
                workspace=workspace,
                turn=result,
                previous_read_context=recent_read_context,
            )
            availability_reference_context = _availability_reference_context_from_turn(
                result,
                previous_context=availability_reference_context,
                verified_read_context=recent_read_context,
            )
            records.append(
                turn_record(
                    result,
                    customer=customer,
                    raw=list(RAW),
                    stages=list(STAGES),
                    active_task_before=previous_task,
                    availability_reference_context=availability_reference_context,
                )
            )
            previous_task = result.active_task
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


def _raw_grouped_action(record: dict[str, object]) -> str | None:
    for capture in reversed(list(record.get("raw_model_structured") or [])):
        if not isinstance(capture, dict) or capture.get("status") != "ok":
            continue
        structured = capture.get("structured")
        if not isinstance(structured, dict):
            continue
        operations = structured.get("operations")
        if not isinstance(operations, list) or not operations or not isinstance(operations[0], dict):
            continue
        return str(operations[0].get("grouped_booking_action") or "") or None
    return None


def _collapsed_service_set(record: dict[str, object]) -> set[str]:
    for stage in record.get("normalization_stages") or []:
        if not isinstance(stage, dict) or stage.get("stage") != "collapse_booking_service_sets":
            continue
        service_sets = stage.get("canonical_service_sets")
        if not isinstance(service_sets, dict):
            continue
        for values in service_sets.values():
            if isinstance(values, list):
                return {str(item) for item in values}
    return set()


def _validate_service_correction(records, ids, *, expected: set[str], action: str) -> None:
    assert len(records) == 2
    before, corrected = records
    assert set(_task_ids(before)) == {ids["underarm"], ids["bikini"]}
    assert _raw_grouped_action(corrected) == action
    assert _collapsed_service_set(corrected) == expected
    assert set(_task_ids(corrected)) == expected
    assert corrected["pending_write"] is None


def validate_c7_replacement(records, ids):
    expected = {ids["underarm"], ids["arms"]}
    _validate_service_correction(records, ids, expected=expected, action="remove_other_components")
    assert ids["bikini"] not in _task_ids(records[-1])


def validate_c7_addition(records, ids):
    expected = {ids["underarm"], ids["bikini"], ids["arms"]}
    _validate_service_correction(records, ids, expected=expected, action="preserve_group")


def validate_c7_removal(records, ids):
    expected = {ids["underarm"]}
    _validate_service_correction(records, ids, expected=expected, action="remove_other_components")


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
    assert len(records) == 3
    assert set(_task_ids(records[0])) == expected

    commercial = records[1]
    assert set(_task_ids(commercial)) == expected
    assert commercial.get("proposal") is None
    active_task = commercial.get("active_task")
    assert isinstance(active_task, dict)
    assert any(
        isinstance(outcome, dict)
        and isinstance(outcome.get("facts"), dict)
        and outcome["facts"].get("booking_commercial_basis_step") is True
        for outcome in commercial.get("outcomes") or []
    )
    commercial_reply = str(commercial.get("reply") or "")
    assert "جنيه" in commercial_reply or "EGP" in commercial_reply

    final = records[2]
    assert set(_task_ids(final)) == expected
    final_task = final.get("active_task")
    assert isinstance(final_task, dict)
    final_authorization = final_task.get("write_authorization")
    assert isinstance(final_authorization, dict)
    assert final_authorization.get("authorized") is True
    proposal = final.get("proposal")
    assert isinstance(proposal, dict), "RA-G5 must create a proposal before any clinic write"
    assert set(proposal.get("service_ids") or []) == expected
    assert len(proposal.get("additional_service_ids") or []) == 1
    assert str(proposal.get("start_at") or "").startswith("2026-10-17T15:00:00")
    assert final["pending_write"] is None



def validate_g7_category_split(records, ids):
    expected = {ids["underarm"], ids["bikini"], ids["dermatology"]}
    assert len(records) == 3
    final = records[-1]
    assert set(_task_ids(final)) == expected
    assert final["pending_write"] is None
    proposal = final.get("proposal")
    assert isinstance(proposal, dict), "RA-G7 must remain proposal-only before confirmation"
    assert set(proposal.get("service_ids") or []) == expected
    visits = proposal.get("visits")
    assert isinstance(visits, list) and len(visits) == 2
    assert all(isinstance(visit, dict) for visit in visits)
    laser_visit = next(
        visit for visit in visits
        if set(visit.get("service_ids") or []) == {ids["underarm"], ids["bikini"]}
    )
    dermatology_visit = next(
        visit for visit in visits
        if visit.get("service_ids") == [ids["dermatology"]]
    )
    assert laser_visit.get("doctor_id") == ids["doctor"]
    assert dermatology_visit.get("doctor_id") == ids["dermatology_doctor"]
    assert laser_visit.get("device_key") == "candela_gentle"
    assert dermatology_visit.get("device_key") is None
    laser_start = datetime.fromisoformat(str(laser_visit["start_at"]))
    laser_end = datetime.fromisoformat(str(laser_visit["end_at"]))
    dermatology_start = datetime.fromisoformat(str(dermatology_visit["start_at"]))
    dermatology_end = datetime.fromisoformat(str(dermatology_visit["end_at"]))
    assert int((laser_end - laser_start).total_seconds() // 60) == 30
    assert int((dermatology_end - dermatology_start).total_seconds() // 60) == 30
    ordered = sorted(
        [(laser_start, laser_end), (dermatology_start, dermatology_end)],
        key=lambda item: item[0],
    )
    assert ordered[0][0].astimezone(CAIRO).strftime("%H:%M") == "18:00"
    assert ordered[1][0] >= ordered[0][1]

def main() -> int:
    if MODE not in {"standalone", "batch1-overlay"}:
        raise RuntimeError(f"Unsupported RA_G_MODE={MODE}")

    if MODE == "standalone":
        run_scenario("RA-C7-REPLACEMENT", [C7_INITIAL, C7_REPLACEMENT], validate_c7_replacement)
        run_scenario("RA-C7-REPLACEMENT-V1", [C7_INITIAL, C7_REPLACEMENT_VARIANT_1], validate_c7_replacement)
        run_scenario("RA-C7-REPLACEMENT-V2", [C7_INITIAL, C7_REPLACEMENT_VARIANT_2], validate_c7_replacement)
        run_scenario("RA-C7-ADDITION", [C7_INITIAL, C7_ADDITION], validate_c7_addition)
        run_scenario("RA-C7-REMOVAL", [C7_INITIAL, C7_REMOVAL], validate_c7_removal)
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
        [
            ORIGINAL,
            "السبت الساعة 6 مساء مناسب، احجزيه ووريني التفاصيل قبل التأكيد النهائي",
            "تمام، السعر مناسب. كملي نفس الحجز يوم السبت الساعة 6 مساء ووريني التفاصيل قبل التأكيد النهائي",
        ],
        validate_g5,
    )
    run_scenario(
        "RA-G6-VISIT-TIME-SELECTION",
        [RA_G6_INITIAL, RA_G6_PRICE, RA_G6_TIME],
        validate_g6_visit_time_selection,
    )
    if MODE == "standalone":
        run_scenario(
            "RA-G7-CATEGORY-SPLIT",
            [RA_G7_INITIAL, RA_G7_BOOK, RA_G7_CONFIRM_DETAILS],
            validate_g7_category_split,
        )
    print(f"RA_G_SUMMARY={{\"mode\":\"{MODE}\",\"status\":\"PASS\"}}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
