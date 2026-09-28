from __future__ import annotations

"""Focused realistic V2 review for read continuity, entity sets, and execution intent.

The suite uses the same staging clinic data as run_live_agent_ux_review but enters through the
shared V2 live facade explicitly. Every conversation owns an outer SQL transaction that is rolled
back. No WhatsApp/n8n delivery is invoked. The script reports transcripts plus deterministic
DB-safety checks; reply quality is meant to be reviewed from the transcript rather than reduced to
brittle phrase matching.
"""

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import scripts.run_live_agent_ux_review as base
from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_v2.live_chat import run_agent_chat as run_agent_chat_v2


OLD_CASES = (
    "general_availability_ranges",
    "unavailable_exact_time",
    "availability_after_six",
    "availability_window",
    "doctor_discovery",
    "package_compare",
    "mixed_language",
    "service_change_mid_flow",
    "book_from_window",
)
NEW_CASES = (
    "time_constraint_replacement_new",
    "nearest_read_only_new",
    "doctor_pair_comparison_new",
    "package_hypothetical_new",
    "reschedule_hypothetical_new",
    "read_then_explicit_book_new",
)
DEFAULT_CASES = OLD_CASES + NEW_CASES


@dataclass
class ReviewResult:
    name: str
    turns: list[base.Turn] = field(default_factory=list)
    db_checks: list[str] = field(default_factory=list)
    error: str | None = None


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--report", default="artifacts/v2-read-semantics-live-review.json")
    parser.add_argument("--case", dest="cases", action="append", default=None)
    return parser.parse_args()


def _appointment_count(db: Session, workspace: Workspace, patient: Patient) -> int:
    return int(
        db.scalar(
            select(func.count(Appointment.id)).where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
        )
        or 0
    )


def _send_v2(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    message: str,
    conversation_id: UUID | None,
):
    started = perf_counter()
    response = run_agent_chat_v2(
        db=db,
        workspace=workspace,
        payload=base._payload(patient.id, message, conversation_id),
    )
    return response, int((perf_counter() - started) * 1000)


def _run_messages(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    first: str,
    second: str,
) -> list[base.Turn]:
    one, d1 = _send_v2(db, workspace, patient, first, None)
    turns = [base.Turn(first, one.reply, one.model, d1)]
    two, d2 = _send_v2(db, workspace, patient, second, one.conversation_id)
    turns.append(base.Turn(second, two.reply, two.model, d2))
    return turns


def _old_case(
    name: str,
    db: Session,
    workspace: Workspace,
) -> tuple[Patient, str, str, list[str]]:
    patient = base._base_patient(db, workspace)
    checks: list[str] = []
    if name == "package_compare":
        selected = base._package_patient(db, workspace)
        if selected is None:
            raise RuntimeError("No usable package patient")
        patient, package = selected
        service = db.scalar(select(Service).where(Service.id == package.service_id))
        service_name = service.name if service is not None else "الخدمة"
        before = _appointment_count(db, workspace, patient)
        first = f"بالنسبة لـ{service_name} أحجز جلسة واحدة ولا أستخدم الباكيدج اللي عندي؟"
        second = "أنا بس بسأل، متحجزش حاجة"
        checks.extend(
            [
                f"appointments_before={before}",
                f"package_status_before={package.status}",
            ]
        )
        return patient, first, second, checks

    first, second, _legacy_check = base._case_messages(name, db, workspace, patient)
    checks.append(f"appointments_before={_appointment_count(db, workspace, patient)}")
    return patient, first, second, checks


def _doctor_pair_context(db: Session, workspace: Workspace):
    catalog, service, _doctor, branch_id, _day, _available = base._booking_context(db, workspace)
    service_id = str(service["id"])
    compatible = [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and row.get("id")
        and service_id in {str(item) for item in (row.get("service_ids") or [])}
    ]
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    usable: list[dict[str, object]] = []
    for doctor in compatible:
        doctor_id = str(doctor["id"])
        found = False
        for offset in range(1, 15):
            result = adapter.get_availability(
                AvailabilityRequest(
                    branch_id=str(branch_id),
                    service_id=service_id,
                    booking_date=today + timedelta(days=offset),
                    doctor_id=doctor_id,
                )
            )
            if result.slots:
                found = True
                break
        if found:
            usable.append(doctor)
        if len(usable) == 2:
            return service, usable[0], usable[1]
    raise RuntimeError("No service has two doctors with near-term availability")


def _replacement_day_for_appointment(
    db: Session,
    workspace: Workspace,
    appointment: Appointment,
) -> date:
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    timezone = ZoneInfo(workspace.timezone)
    original_day = appointment.start_at.astimezone(timezone).date()
    for offset in range(1, 15):
        day = original_day + timedelta(days=offset)
        result = adapter.get_availability(
            AvailabilityRequest(
                branch_id=str(appointment.branch_id),
                service_id=str(appointment.service_id),
                booking_date=day,
                doctor_id=str(appointment.doctor_id) if appointment.doctor_id else None,
                exclude_appointment_id=str(appointment.id),
            )
        )
        if result.slots:
            return day
    raise RuntimeError("No replacement day available for hypothetical reschedule")


def _new_case(
    name: str,
    db: Session,
    workspace: Workspace,
) -> tuple[Patient, str, str, list[str]]:
    patient = base._base_patient(db, workspace)
    before = _appointment_count(db, workspace, patient)
    catalog, service, doctor, _branch_id, day, available = base._booking_context(db, workspace)
    del catalog
    service_name = str(service.get("name") or "الخدمة")
    doctor_name = str(doctor.get("name") or "الدكتور")
    date_text = day.isoformat()
    local_tz = ZoneInfo(available.timezone)
    local_slot = available.slots[0].start_at.astimezone(local_tz)
    checks = [f"appointments_before={before}"]

    if name == "time_constraint_replacement_new":
        return (
            patient,
            f"وريني مواعيد {service_name} يوم {date_text} بعد الساعة 7 بالليل",
            "طب غيرها: عايز اللي قبل الساعة 4 العصر بدل كده",
            checks,
        )
    if name == "nearest_read_only_new":
        return (
            patient,
            f"هل {service_name} مع {doctor_name} يوم {date_text} الساعة 14:07 متاح؟ أنا بس بسأل",
            "لو مش متاح وريني أقرب وقت للساعة دي بس، من غير حجز",
            checks,
        )
    if name == "doctor_pair_comparison_new":
        service_row, doctor_a, doctor_b = _doctor_pair_context(db, workspace)
        return (
            patient,
            f"بالنسبة لـ{service_row.get('name') or service_name}، مين متاح أقرب: {doctor_a.get('name')} ولا {doctor_b.get('name')}؟",
            "قولي الأقرب فيهم بس، أنا مش بحجز دلوقتي",
            checks,
        )
    if name == "package_hypothetical_new":
        selected = base._package_patient(db, workspace)
        if selected is None:
            raise RuntimeError("No usable package patient")
        patient, package = selected
        service_row = db.scalar(select(Service).where(Service.id == package.service_id))
        package_service_name = service_row.name if service_row is not None else "الخدمة"
        before = _appointment_count(db, workspace, patient)
        return (
            patient,
            f"لو محتاج جلسة {package_service_name}، أستخدم من الباكيدج ولا أدفع جلسة منفصلة؟ أنا بس بقارن",
            "قولي المتبقي عندي والفرق لو متاح، ومتحجزش أي حاجة",
            [f"appointments_before={before}", f"package_status_before={package.status}"],
        )
    if name == "reschedule_hypothetical_new":
        rows = base._seed_upcoming(db, workspace, patient, 1)
        row = rows[0]
        service_row = db.scalar(select(Service).where(Service.id == row.service_id))
        existing_service_name = service_row.name if service_row is not None else service_name
        target_day = _replacement_day_for_appointment(db, workspace, row)
        return (
            patient,
            f"لو حبيت أغير معاد {existing_service_name} بتاعي ليوم {target_day.isoformat()}، إيه المتاح؟ متغيرش حاجة دلوقتي",
            "تمام أنا بس بشوف الاختيارات، سيب معادي زي ما هو",
            [f"original_appointment_id={row.id}", f"original_status_before={row.status}"],
        )
    if name == "read_then_explicit_book_new":
        return (
            patient,
            f"إيه المتاح لـ{service_name} مع {doctor_name} يوم {date_text}؟",
            f"تمام، احجزلي الساعة {local_slot.strftime('%H:%M')}",
            checks,
        )
    raise KeyError(name)


def _execute(engine, slug: str, name: str) -> ReviewResult:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    result = ReviewResult(name=name)
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == slug))
        if workspace is None:
            raise RuntimeError("Workspace not found")
        if name in OLD_CASES:
            patient, first, second, checks = _old_case(name, db, workspace)
        elif name in NEW_CASES:
            patient, first, second, checks = _new_case(name, db, workspace)
        else:
            raise KeyError(name)

        before = _appointment_count(db, workspace, patient)
        result.turns = _run_messages(db, workspace, patient, first, second)
        after = _appointment_count(db, workspace, patient)
        result.db_checks = [*checks, f"appointments_after={after}", f"appointment_delta={after - before}"]

        if name == "reschedule_hypothetical_new":
            original_id = UUID(result.db_checks[0].split("=", 1)[1])
            original = db.get(Appointment, original_id)
            result.db_checks.append(
                f"original_status_after={original.status if original is not None else 'missing'}"
            )
        if name in {"package_compare", "package_hypothetical_new"}:
            result.db_checks.append("expected_write_delta=0")
        if name in {
            "time_constraint_replacement_new",
            "nearest_read_only_new",
            "doctor_pair_comparison_new",
            "reschedule_hypothetical_new",
        }:
            result.db_checks.append("expected_write_delta=0")
        if name in {"book_from_window", "read_then_explicit_book_new"}:
            result.db_checks.append("expected_write_delta=1")
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
    return result


def main() -> int:
    args = _args()
    if not settings.agent_v2_live_enabled:
        raise RuntimeError(
            "Focused V2 live review requires AGENT_V2_LIVE_ENABLED=true; refusing to fall back to V1."
        )
    names = tuple(args.cases or DEFAULT_CASES)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results: list[ReviewResult] = []
    try:
        for index, name in enumerate(names, start=1):
            print(f"[{index:02d}/{len(names)}] {name}", flush=True)
            result = _execute(engine, args.workspace_slug, name)
            results.append(result)
            # One compact line per conversation is intentional: Railway drops bursts above 500 logs/s.
            print(json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        engine.dispose()

    payload = {
        "started_at": datetime.now(UTC).isoformat(),
        "workspace_slug": args.workspace_slug,
        "conversation_count": len(results),
        "runtime": "v2",
        "database_writes_persisted": False,
        "whatsapp_or_n8n_used": False,
        "results": [asdict(item) for item in results],
    }
    path = Path(args.report)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {path}", flush=True)
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
