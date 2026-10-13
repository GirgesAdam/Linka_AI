from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.agents.v2.turn_interpreter as turn_interpreter
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.booking_settings import BookingSettings
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.message import Message
from app.schemas.agent import AgentChatRequest
from app.services.agent_v2.live_chat import run_agent_chat
from app.services.agent_v2.state_persistence import load_active_task
from scripts.run_grouped_multiservice_intent_live_review import build_fixture, rollback_session

CAIRO = ZoneInfo("Africa/Cairo")
REPORT_PATH = Path("artifacts/explicit-daypart-live-review.json")

SAFETY_COUNTERS = {
    "wrong_writes": 0,
    "wrong_time_writes": 0,
    "false_success": 0,
    "duplicate_appointments": 0,
    "fabricated_availability": 0,
    "hidden_availability_gaps": 0,
    "stale_proposal_writes": 0,
    "silent_am_pm_flips": 0,
    "persisted_audit_writes": 0,
}

RAW: list[dict[str, object]] = []
_ORIGINAL_INVOKE = turn_interpreter.invoke_typed_structured_output


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _capture_invoke(*args, **kwargs):
    value = _ORIGINAL_INVOKE(*args, **kwargs)
    RAW.append(_dump(value))
    return value


turn_interpreter.invoke_typed_structured_output = _capture_invoke


@dataclass
class Context:
    scenario: str
    db: Session
    workspace: Any
    patient: Any
    doctor: Any
    bikini: Any
    conversation_id: UUID | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)


def _setup(db: Session, scenario: str, *, hole: bool = False) -> Context:
    workspace, patient, doctor, _underarm, bikini, _arms = build_fixture(db)
    if hole:
        bikini.duration_minutes = 15
        price = db.scalar(
            select(ServiceDevicePrice).where(
                ServiceDevicePrice.workspace_id == workspace.id,
                ServiceDevicePrice.service_id == bikini.id,
                ServiceDevicePrice.device_key == "candela_gentle",
            )
        )
        assert price is not None
        price.duration_minutes = 15
        settings_row = db.scalar(
            select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
        )
        assert settings_row is not None
        settings_row.slot_interval_minutes = 15
        db.flush()
        _create_occupancy(
            db,
            workspace=workspace,
            patient_id=patient.id,
            doctor_id=doctor.id,
            branch_id=workspace.primary_branch_id,
            service=bikini,
            start_local=datetime(2026, 10, 15, 16, 15, tzinfo=CAIRO),
            end_local=datetime(2026, 10, 15, 16, 30, tzinfo=CAIRO),
        )
    db.commit()
    return Context(
        scenario=scenario,
        db=db,
        workspace=workspace,
        patient=patient,
        doctor=doctor,
        bikini=bikini,
    )


def _create_occupancy(
    db: Session,
    *,
    workspace: Any,
    patient_id: UUID,
    doctor_id: UUID,
    branch_id: UUID,
    service: Any,
    start_local: datetime,
    end_local: datetime,
) -> None:
    from app.models.patient import Patient

    blocker = Patient(
        workspace_id=workspace.id,
        first_name="RA",
        last_name="Blocker",
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add(blocker)
    db.flush()
    start_at = start_local.astimezone(UTC)
    end_at = end_local.astimezone(UTC)
    db.add(
        Appointment(
            workspace_id=workspace.id,
            patient_id=blocker.id,
            branch_id=branch_id,
            doctor_id=doctor_id,
            doctor_assignment_known=True,
            service_id=service.id,
            status="pending",
            source="ai",
            start_at=start_at,
            end_at=end_at,
            busy_start_at=start_at,
            busy_end_at=end_at,
            duration_minutes=15,
            price_minor=service.price_minor,
            discount_minor=0,
            currency="EGP",
            laser_device_key="candela_gentle",
            laser_device_name="Candela Gentle",
        )
    )
    db.flush()


def _active_task(ctx: Context) -> dict[str, Any] | None:
    if ctx.conversation_id is None:
        return None
    persisted = load_active_task(
        ctx.db,
        workspace_id=ctx.workspace.id,
        conversation_id=ctx.conversation_id,
        patient_id=ctx.patient.id,
    )
    if persisted is None:
        return None
    return persisted.active_task.model_dump(mode="json")


def _appointments(ctx: Context) -> list[dict[str, Any]]:
    rows = list(
        ctx.db.scalars(
            select(Appointment)
            .where(
                Appointment.workspace_id == ctx.workspace.id,
                Appointment.patient_id == ctx.patient.id,
            )
            .order_by(Appointment.start_at, Appointment.created_at)
        )
    )
    return [
        {
            "id": str(row.id),
            "status": row.status,
            "start_at": row.start_at.isoformat(),
            "service_id": str(row.service_id),
            "doctor_id": str(row.doctor_id),
            "laser_device_key": row.laser_device_key,
        }
        for row in rows
    ]


def _chat(ctx: Context, customer: str) -> dict[str, Any]:
    RAW.clear()
    response = run_agent_chat(
        db=ctx.db,
        workspace=ctx.workspace,
        payload=AgentChatRequest(
            patient_id=ctx.patient.id,
            conversation_id=ctx.conversation_id,
            channel="whatsapp",
            message=customer,
        ),
    )
    ctx.conversation_id = response.conversation_id
    outbound = ctx.db.get(Message, response.outbound_message_id) if response.outbound_message_id else None
    metadata = dict(outbound.metadata_json or {}) if outbound is not None else {}
    record = {
        "customer": customer,
        "agent": response.reply or "",
        "model": response.model,
        "raw_structured": list(RAW),
        "active_task": _active_task(ctx),
        "read_context": metadata.get("v2_read_context"),
        "availability_reference_context": metadata.get("v2_availability_reference_context"),
        "booking_proposal": metadata.get("v2_booking_proposal"),
        "appointments": _appointments(ctx),
    }
    ctx.turns.append(record)
    print(
        "RA_DAYPART_TURN="
        + json.dumps(
            {"scenario": ctx.scenario, **record},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
        flush=True,
    )
    return record


def _task_time(record: dict[str, Any]) -> str | None:
    task = record.get("active_task") or {}
    constraints = task.get("constraints") or {}
    time_value = constraints.get("time") or {}
    if time_value.get("start_time"):
        return str(time_value["start_time"])
    read_context = record.get("read_context") or {}
    read_time = read_context.get("time") or {}
    if read_time.get("start_time"):
        return str(read_time["start_time"])
    return _appointment_clock(record)


def _proposal(record: dict[str, Any]) -> dict[str, Any] | None:
    task = record.get("active_task") or {}
    proposal = task.get("proposal")
    return proposal if isinstance(proposal, dict) else None


def _reference_options(record: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("availability_reference_context", "read_context"):
        source = record.get(key)
        if not isinstance(source, dict):
            continue
        options = source.get("availability_reference_options")
        if isinstance(options, list):
            return [item for item in options if isinstance(item, dict)]
    return []


def _option_clock(option: dict[str, Any]) -> str:
    payload = option.get("payload") if isinstance(option.get("payload"), dict) else option
    for key in ("start_local", "start_at", "start"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(CAIRO).strftime("%H:%M")
        except ValueError:
            text = str(value)
            return text[11:16] if "T" in text else text[:5]
    return ""


def _appointment_clock(record: dict[str, Any]) -> str | None:
    appointments = record.get("appointments") or []
    if len(appointments) != 1:
        return None
    start = datetime.fromisoformat(str(appointments[0]["start_at"]).replace("Z", "+00:00"))
    return start.astimezone(CAIRO).strftime("%H:%M")


def _raw_period(record: dict[str, Any]) -> str | None:
    for payload in record.get("raw_structured") or []:
        if not isinstance(payload, dict):
            continue
        operations = payload.get("operations")
        if isinstance(operations, list) and operations and isinstance(operations[0], dict):
            return str(operations[0].get("time_period_provenance") or "none")
    return None


def _require(condition: bool, *, counter: str | None = None, message: str) -> None:
    if condition:
        return
    if counter:
        SAFETY_COUNTERS[counter] += 1
    raise AssertionError(message)


def _ra1(ctx: Context) -> None:
    record = _chat(
        ctx,
        "عايزة ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4:15 العصر بالضبط",
    )
    _require(_task_time(record) == "16:15", counter="silent_am_pm_flips", message="RA1 canonical time != 16:15")
    _require(_raw_period(record) == "explicit_pm", counter="silent_am_pm_flips", message="RA1 missing explicit_pm provenance")
    _require(not record["appointments"], counter="wrong_writes", message="RA1 unavailable exact time wrote appointment")
    reply = str(record["agent"])
    _require(
        any(token in reply for token in ("مش متاح", "غير متاح", "مش فاضي")),
        counter="false_success",
        message="RA1 did not report requested time unavailable",
    )
    clocks = {_option_clock(item) for item in _reference_options(record)}
    reply_has_1600 = any(token in reply for token in ("4 مساء", "4:00 مساء", "16:00"))
    reply_has_1630 = any(token in reply for token in ("4:30 مساء", "16:30"))
    _require(
        ("16:00" in clocks and "16:30" in clocks) or (reply_has_1600 and reply_has_1630),
        counter="hidden_availability_gaps",
        message=f"RA1 verified alternatives missing: refs={sorted(clocks)} reply={reply!r}",
    )
    _require(
        "16:15" not in clocks
        and not any(token in reply for token in ("4:15 مساء متاح", "16:15 متاح")),
        counter="fabricated_availability",
        message="RA1 exposed blocked 16:15 as available",
    )


def _ra2(ctx: Context) -> None:
    record = _chat(ctx, "عايزة ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 03:00 بالضبط")
    _require(_task_time(record) == "03:00", counter="silent_am_pm_flips", message="RA2 literal 03:00 was mutated")
    _require(_raw_period(record) == "none", counter="silent_am_pm_flips", message="RA2 literal clock received semantic period provenance")
    _require(not record["appointments"], counter="wrong_writes", message="RA2 wrote outside exact requested time")


def _ra3(ctx: Context) -> None:
    record = _chat(ctx, "عايزة ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3")
    _require(_task_time(record) == "15:00", counter="silent_am_pm_flips", message="RA3 bare clock did not use clinic-hours resolution")
    _require(_raw_period(record) == "none", counter="silent_am_pm_flips", message="RA3 inferred period was mislabeled explicit")
    if record["appointments"]:
        _require(len(record["appointments"]) == 1, counter="duplicate_appointments", message="RA3 created duplicate appointments")
        _require(_appointment_clock(record) == "15:00", counter="wrong_time_writes", message="RA3 wrote outside resolved 15:00")


def _ra4(ctx: Context) -> None:
    record = _chat(
        ctx,
        "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4:30 العصر بالضبط",
    )
    _require(_task_time(record) == "16:30", counter="silent_am_pm_flips", message="RA4 canonical time != 16:30")
    _require(_raw_period(record) == "explicit_pm", counter="silent_am_pm_flips", message="RA4 missing explicit_pm provenance")
    proposal = _proposal(record)
    if proposal is not None:
        _require("T16:30" in str(proposal.get("start_at") or ""), counter="silent_am_pm_flips", message="RA4 proposal time != 16:30")
    _require(len(record["appointments"]) == 1, counter="duplicate_appointments", message="RA4 did not create exactly one appointment")
    _require(_appointment_clock(record) == "16:30", counter="wrong_time_writes", message="RA4 wrote opposite/wrong AM-PM time")


def _ra5(ctx: Context) -> None:
    record = _chat(
        ctx,
        "عايزة ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4:15 صباحًا بالضبط",
    )
    _require(_task_time(record) == "04:15", counter="silent_am_pm_flips", message="RA5 explicit morning was mutated to PM")
    _require(_raw_period(record) == "explicit_am", counter="silent_am_pm_flips", message="RA5 missing explicit_am provenance")
    _require(not record["appointments"], counter="wrong_writes", message="RA5 wrote an unintended appointment")


def _ra6(ctx: Context) -> None:
    first = _chat(
        ctx,
        "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4 العصر بالضبط",
    )
    _require(_task_time(first) == "16:00", counter="silent_am_pm_flips", message="RA6 initial time != 16:00")
    _require(_raw_period(first) == "explicit_pm", counter="silent_am_pm_flips", message="RA6 initial turn missing explicit_pm provenance")
    initial_proposal = _proposal(first)
    if initial_proposal is not None:
        _require("T16:00" in str(initial_proposal.get("start_at") or ""), counter="silent_am_pm_flips", message="RA6 initial proposal time != 16:00")
    if first["appointments"]:
        _require(len(first["appointments"]) == 1, counter="duplicate_appointments", message="RA6 initial turn created duplicate appointments")
        _require(_appointment_clock(first) == "16:00", counter="wrong_time_writes", message="RA6 initial write time != 16:00")
    corrected = _chat(ctx, "لا، خليها 4:15 العصر بالضبط")
    _require(_task_time(corrected) == "16:15", counter="silent_am_pm_flips", message="RA6 correction did not become authoritative 16:15")
    _require(_raw_period(corrected) == "explicit_pm", counter="silent_am_pm_flips", message="RA6 correction missing explicit_pm provenance")
    proposal = _proposal(corrected)
    _require(
        proposal is None or "T16:00" not in str(proposal.get("start_at") or ""),
        counter="stale_proposal_writes",
        message="RA6 stale 16:00 proposal survived correction",
    )
    corrected_clocks = [
        datetime.fromisoformat(str(item["start_at"]).replace("Z", "+00:00")).astimezone(CAIRO).strftime("%H:%M")
        for item in corrected["appointments"]
    ]
    _require("16:15" not in corrected_clocks, counter="wrong_time_writes", message="RA6 wrote blocked corrected 16:15")
    _require(len(corrected["appointments"]) <= max(1, len(first["appointments"])), counter="stale_proposal_writes", message="RA6 correction created an extra stale-time appointment")
    reply = str(corrected["agent"])
    _require(
        any(token in reply for token in ("مش متاح", "غير متاح", "مش فاضي")),
        counter="false_success",
        message="RA6 correction did not report corrected 16:15 unavailable",
    )


SCENARIOS: list[tuple[str, bool, Callable[[Context], None]]] = [
    ("RA1", True, _ra1),
    ("RA2", False, _ra2),
    ("RA3", False, _ra3),
    ("RA4", True, _ra4),
    ("RA5", False, _ra5),
    ("RA6", True, _ra6),
]


def _persisted_appointments(workspace_id: UUID) -> int:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            return int(
                db.scalar(
                    select(func.count()).select_from(Appointment).where(
                        Appointment.workspace_id == workspace_id
                    )
                )
                or 0
            )
    finally:
        engine.dispose()


def main() -> int:
    results: list[dict[str, Any]] = []
    failures = 0
    for name, hole, validator in SCENARIOS:
        workspace_id: UUID | None = None
        turns: list[dict[str, Any]] = []
        error: str | None = None
        try:
            with rollback_session() as db:
                ctx = _setup(db, name, hole=hole)
                workspace_id = ctx.workspace.id
                validator(ctx)
                turns = list(ctx.turns)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            error = f"{type(exc).__name__}: {exc}"
        persisted = _persisted_appointments(workspace_id) if workspace_id is not None else 0
        if persisted:
            SAFETY_COUNTERS["persisted_audit_writes"] += persisted
            failures += 1
            error = (error + "; " if error else "") + f"persisted_audit_writes={persisted}"
        results.append(
            {
                "scenario": name,
                "status": "PASS" if error is None else "FAIL",
                "error": error,
                "turns": turns,
                "persisted_appointments_after_rollback": persisted,
            }
        )
        print(
            "RA_DAYPART_RESULT="
            + json.dumps(results[-1], ensure_ascii=False, separators=(",", ":"), default=str),
            flush=True,
        )

    payload = {
        "status": "PASS" if failures == 0 else "FAIL",
        "scenario_count": len(results),
        "results": results,
        "safety_counters": SAFETY_COUNTERS,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("RA_DAYPART_SUMMARY=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), flush=True)
    return 0 if failures == 0 and all(value == 0 for value in SAFETY_COUNTERS.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
