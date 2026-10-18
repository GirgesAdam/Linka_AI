from __future__ import annotations

import json
import os
import subprocess
import traceback
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import run_grouped_multiservice_intent_live_review as rag
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.conversation import Conversation
from app.services.agent_v2.live_chat import (
    _availability_reference_context_from_turn,
    _verified_read_context_from_turn,
)
from app.services.agent_v2.orchestrator import orchestrate_v2_turn


def _build_fixture(db):
    """Extend main's legacy six-object fixture without changing production code."""
    fixture = rag.build_fixture(db)
    if len(fixture) == 8:
        return fixture
    if len(fixture) != 6:
        raise RuntimeError(f"unsupported main fixture shape: {len(fixture)} items")

    workspace, patient, doctor, underarm, bikini, arms = fixture
    branch = db.scalar(select(Branch).where(Branch.id == workspace.primary_branch_id))
    if branch is None:
        raise RuntimeError("main fixture did not create a primary branch")

    suffix = uuid4().hex[:10]
    dermatology_staff = rag.Staff(
        workspace_id=workspace.id,
        first_name="Dermatology",
        last_name="Doctor",
        is_active=True,
    )
    dermatology = rag.Service(
        workspace_id=workspace.id,
        name="كشف جلدية",
        slug=f"dermatology-consultation-{suffix}",
        operational_category="dermatology",
        duration_minutes=30,
        price_minor=100_000,
        currency="EGP",
        requires_laser_device=False,
        is_active=True,
    )
    db.add_all([dermatology_staff, dermatology])
    db.flush()

    dermatology_doctor = rag.Doctor(
        workspace_id=workspace.id,
        staff_id=dermatology_staff.id,
        specialization="Dermatology",
        booking_enabled=True,
        is_active=True,
    )
    db.add(dermatology_doctor)
    db.flush()

    db.add(
        rag.DoctorBranch(
            workspace_id=workspace.id,
            doctor_id=dermatology_doctor.id,
            branch_id=branch.id,
            is_primary=True,
            is_active=True,
        )
    )
    db.add(
        rag.DoctorService(
            workspace_id=workspace.id,
            doctor_id=dermatology_doctor.id,
            service_id=dermatology.id,
            is_active=True,
        )
    )
    for weekday in range(7):
        db.add(
            rag.DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=dermatology_doctor.id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=rag.time(10),
                end_time=rag.time(22),
            )
        )
    db.flush()
    return workspace, patient, doctor, dermatology_doctor, underarm, bikini, arms, dermatology


def _dump_trace(value):
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (dict, list, str, int, float, bool)):
        return value
    return str(value)


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    messages: tuple[str, ...]
    fixture_variant: str | None = None


RW1 = (
    "أنا عايزة أعمل ليزر الإبط والبكيني مع بعض السبت، خليهم في نفس الزيارة وعلى كانديلا لو ينفع، "
    "وشوفيلي ميعاد بعد الساعة 5 بس متحجزيش دلوقتي."
)
RW2 = (
    "عايزة الإبط والبكيني مع بعض السبت، وريني المواعيد بس.",
    "طب ضيفي Full Arms معاهم كمان.",
    "لا معلش، شيل البكيني وخلي الإبط وFull Arms بس.",
)
RW3 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني كل التفاصيل قبل ما تأكدي أي حاجة.",
    "الدكتورة مين والسعر كام؟",
    "تمام، وريني تفاصيل الميعاد تاني بس لسه متحجزيش.",
)
RW4 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني التفاصيل قبل التأكيد.",
    "خليها الساعة 7 بدل 6.",
)
RW5 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني التفاصيل قبل التأكيد.",
    "تمام أكدي الحجز، بس خليها الساعة 7 بدل 6.",
)
RW6 = (
    "أنا عايزة أعمل الإبط والبكيني، وبعدهم كشف جلدية في نفس المشوار يوم السبت، "
    "خليهم ورا بعض ووريني التفاصيل قبل ما تحجزي.",
    "الساعة 6 مناسبة.",
    "دكتور الكشف مين؟",
)
RW7 = (
    RW6[0],
    "الساعة 6 مناسبة.",
    "خلي بداية الزيارة الساعة 7 بدل 6.",
)
RW8 = (
    "عايزة الإبط والبكيني السبت الساعة 6.",
    "طب السبت اللي بعده الساعة 6؟",
)
RW9 = ("السبت الساعة 3 ينفع للإبط والبكيني؟ وريني التفاصيل قبل الحجز.",)
RW10 = (
    "عايزة الإبط والبكيني السبت الساعة 6، أي جهاز مناسب عندكم تمام.",
    "خليها Candela.",
)
RW11 = (
    "عايزة الإبط والبكيني السبت الساعة 6، وريني التفاصيل الأول.",
    "أيوه تمام، أكدي نفس الحجز زي ما هو.",
)


SCENARIOS = (
    ScenarioSpec("RW-1", (RW1,)),
    ScenarioSpec("RW-2", RW2),
    ScenarioSpec("RW-3", RW3),
    ScenarioSpec("RW-4", RW4),
    ScenarioSpec("RW-5", RW5),
    ScenarioSpec("RW-6", RW6),
    ScenarioSpec("RW-7", RW7),
    ScenarioSpec("RW-8", RW8, fixture_variant="busy_2026_10_17_1800"),
    ScenarioSpec("RW-9", RW9),
    ScenarioSpec("RW-10", RW10, fixture_variant="second_laser_device"),
    ScenarioSpec("RW-11", RW11),
)


def _seed_busy_exact_slot(db, *, workspace, patient, doctor, underarm) -> None:
    branch = db.scalar(select(Branch).where(Branch.id == workspace.primary_branch_id))
    assert branch is not None
    start = datetime(2026, 10, 17, 18, 0, tzinfo=rag.CAIRO)
    end = start + timedelta(minutes=30)
    db.add(
        Appointment(
            workspace_id=workspace.id,
            patient_id=patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            doctor_assignment_known=True,
            is_quick_booking=False,
            service_id=underarm.id,
            status="confirmed",
            source="staff",
            start_at=start,
            end_at=end,
            busy_start_at=start,
            busy_end_at=end,
            duration_minutes=30,
            price_minor=50_000,
            discount_minor=0,
            currency="EGP",
            laser_device_key="candela_gentle",
            laser_device_name="Candela Gentle",
            payment_status="unknown",
            payment_method="unknown",
            billing_context="standard",
        )
    )
    db.flush()


def _add_second_laser_device(db, *, workspace, underarm, bikini) -> None:
    for service, price in ((underarm, 45_000), (bikini, 65_000)):
        rag._device_price(
            db,
            workspace=workspace,
            service=service,
            device_key="prime_lase",
            device_name="Prime Lase",
            price_minor=price,
            duration_minutes=30,
        )
    db.flush()


def _run_scenario(spec: ScenarioSpec) -> dict[str, object]:
    """Capture the legacy main behavior; do not apply new-branch validators."""
    records: list[dict[str, object]] = []
    before: int | None = None
    after: int | None = None
    ids: dict[str, str] = {}
    try:
        with rag.rollback_session() as db:
            (
                workspace,
                patient,
                doctor,
                dermatology_doctor,
                underarm,
                bikini,
                arms,
                dermatology,
            ) = _build_fixture(db)
            ids = {
                "underarm": str(underarm.id),
                "bikini": str(bikini.id),
                "arms": str(arms.id),
                "dermatology": str(dermatology.id),
                "doctor": str(doctor.id),
                "dermatology_doctor": str(dermatology_doctor.id),
            }
            if spec.fixture_variant == "busy_2026_10_17_1800":
                _seed_busy_exact_slot(
                    db,
                    workspace=workspace,
                    patient=patient,
                    doctor=doctor,
                    underarm=underarm,
                )
            elif spec.fixture_variant == "second_laser_device":
                _add_second_laser_device(
                    db,
                    workspace=workspace,
                    underarm=underarm,
                    bikini=bikini,
                )

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
                    started_at=rag.NOW,
                    last_message_at=rag.NOW,
                )
            )
            db.flush()

            history = []
            previous_task = None
            recent_read_context: dict[str, object] | None = None
            availability_reference_context: dict[str, object] | None = None
            before = rag.appointment_count(db, workspace.id)

            for index, customer in enumerate(spec.messages, start=1):
                history.append(HumanMessage(content=customer))
                rag.RAW.clear()
                rag.STAGES.clear()
                result = orchestrate_v2_turn(
                    db=db,
                    workspace=workspace,
                    patient=patient,
                    conversation_id=conversation_id,
                    run_id=uuid4(),
                    history=history,
                    local_now=rag.NOW,
                    timezone_name=workspace.timezone,
                    clinic_name=workspace.name,
                    turn_id=f"main-baseline-{spec.name.lower()}-{index}",
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
                record = rag.turn_record(
                    result,
                    customer=customer,
                    raw=list(rag.RAW),
                    stages=list(rag.STAGES),
                    active_task_before=previous_task,
                )
                # Main's older diagnostic dumper stringifies dataclass traces. Preserve
                # structured evidence in this audit-only runner without changing main.
                record["traces"] = [_dump_trace(trace) for trace in result.traces]
                record["recent_read_context"] = recent_read_context
                record["availability_reference_context"] = availability_reference_context
                record["appointment_count_after_turn"] = rag.appointment_count(db, workspace.id)
                records.append(record)
                previous_task = result.active_task
                if result.reply:
                    history.append(AIMessage(content=result.reply))

            after = rag.appointment_count(db, workspace.id)
            payload = {
                "scenario": spec.name,
                "capture_status": "CAPTURED",
                "ids": ids,
                "appointment_count_before": before,
                "appointment_count_after": after,
                "appointment_delta": after - before
                if before is not None and after is not None
                else None,
                "appointment_count_unchanged": before == after
                if before is not None and after is not None
                else None,
                "turns": records,
            }
            print(
                "MAIN_BASELINE_RW_RESULT="
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                flush=True,
            )
            return {
                "scenario": spec.name,
                "status": "CAPTURED",
                "error": None,
                "turns_captured": len(records),
                "appointment_delta": after - before
                if before is not None and after is not None
                else None,
            }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        payload = {
            "scenario": spec.name,
            "capture_status": "RUNTIME_EXCEPTION",
            "ids": ids,
            "appointment_count_before": before,
            "appointment_count_after": after,
            "appointment_delta": after - before
            if before is not None and after is not None
            else None,
            "turns": records,
            "error": error,
            "traceback": traceback.format_exc(),
        }
        print(
            "MAIN_BASELINE_RW_RESULT="
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            flush=True,
        )
        return {
            "scenario": spec.name,
            "status": "RUNTIME_EXCEPTION",
            "error": error,
            "turns_captured": len(records),
            "appointment_delta": after - before
            if before is not None and after is not None
            else None,
        }


def _git_metadata() -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[2]

    def git(*args: str) -> str | None:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    main_sha = os.environ.get("BASELINE_MAIN_SHA") or git("rev-parse", "HEAD^1")
    audit_sha = os.environ.get("AUDIT_BRANCH_SHA") or git("rev-parse", "HEAD^2")
    diff_spec = f"{main_sha}...{audit_sha}" if main_sha and audit_sha else "HEAD^1..HEAD^2"
    return {
        "checkout_head": git("rev-parse", "HEAD"),
        "main_baseline_sha": main_sha,
        "audit_branch_sha": audit_sha,
        "audit_diff_files": (git("diff", "--name-only", diff_spec) or "").splitlines(),
    }


def main() -> int:
    metadata = _git_metadata()
    print(
        "MAIN_BASELINE_AUDIT_METADATA="
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str),
        flush=True,
    )
    results = [_run_scenario(spec) for spec in SCENARIOS]
    summary = {
        "mode": "capture_only_no_product_pass_fail_assertions",
        "main_baseline_parent_sha": metadata.get("main_baseline_parent_sha"),
        "total_scenarios": len(SCENARIOS),
        "scenarios_attempted": len(results),
        "fully_captured": sum(item["status"] == "CAPTURED" for item in results),
        "runtime_exception_scenarios": [
            item for item in results if item["status"] == "RUNTIME_EXCEPTION"
        ],
        "turn_records_captured": sum(int(item.get("turns_captured") or 0) for item in results),
        "results": results,
    }
    print(
        "MAIN_BASELINE_REAL_WORLD_AUDIT_SUMMARY="
        + json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str),
        flush=True,
    )
    return 0 if len(results) == len(SCENARIOS) and summary["turn_records_captured"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
