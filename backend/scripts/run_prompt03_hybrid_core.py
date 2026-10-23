from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import v2_benchmark_core as core
import v2_benchmark_scenarios as scenarios
from app.agents.availability_presentation import availability_windows_from_slots
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.service import Service
from app.models.workspace import Workspace

TARGET_HEAD = "8dd566a10bb49edb8238f0ce8066c0e346f367f4"
REPORT = Path("artifacts/prompt03-hybrid-core.json")
PAGE_SIZE = 4
_PREFLIGHT_PROVIDER_CALLS = 0

ANCHORS = (
    ("existing_package_booking", "A1", 2, "existing package booking"),
    ("package_purchase", "A2", 2, "package purchase"),
    ("change_time_during_booking", "A3", 2, "time correction during booking"),
    ("core_nearest_availability_continuation", "A4", 2, "nearest availability continuation"),
    ("core_normal_booking", "A5", 3, "normal booking + confirmation"),
)
FRESH = (
    ("prompt03_f1_question_to_booking", "F1", 2, "question to booking execution"),
    ("prompt03_f2_multi_dimension_availability_edit", "F2", 2, "multi-dimension availability edit"),
    ("prompt03_f3_next_after_continuation", "F3", 3, "next after continuation"),
    ("prompt03_f4_service_correction_active_booking", "F4", 2, "service correction during active booking"),
    ("prompt03_f5_package_side_question_purchase", "F5", 3, "package offer side question purchase"),
    ("prompt03_f6_reject_existing_package", "F6", 1, "explicit existing-package rejection"),
    ("prompt03_f7_same_date_reschedule_time", "F7", 1, "same-date reschedule time change"),
    ("prompt03_f8_fact_challenge_after_side_question", "F8", 3, "appointment fact challenge after side question"),
    ("prompt03_f9_multiple_offers_explicit_choice", "F9", 2, "multiple offers explicit package choice"),
    ("prompt03_f10_purchase_and_book_same_turn", "F10", 1, "package purchase plus first booking same turn"),
    ("prompt03_f11_fresh_task_after_active_booking", "F11", 2, "fresh task after active booking"),
    ("prompt03_f12_new_availability_reference_language", "F12", 2, "new availability-reference language"),
)
PORTFOLIO = ANCHORS + FRESH
ORDER = [row[0] for row in PORTFOLIO]
META = {
    row[0]: {
        "label": row[1], "planned_turns": row[2], "topic": row[3],
        "population": "anchor" if row in ANCHORS else "fresh",
    }
    for row in PORTFOLIO
}


def install_provider_forbidden_guard() -> None:
    global _PREFLIGHT_PROVIDER_CALLS
    _PREFLIGHT_PROVIDER_CALLS = 0

    def forbidden_generate(self, *args, **kwargs):
        global _PREFLIGHT_PROVIDER_CALLS
        _PREFLIGHT_PROVIDER_CALLS += 1
        raise RuntimeError("Provider invocation is forbidden during PROMPT-03 preflight")

    ChatOpenAI._generate = forbidden_generate


def verify_output_path() -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    probe = REPORT.parent / ".prompt03-preflight-write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def clone(prepared: core.PreparedScenario, messages: list[str], *observations: str) -> core.PreparedScenario:
    return core.PreparedScenario(
        patient=prepared.patient,
        messages=list(messages),
        observations=[*prepared.observations, *[x for x in observations if x]],
        post=prepared.post,
    )


def later_same_doctor_slot(db: Session, workspace: Workspace, *, service_id: Any, doctor_id: Any, after_day):
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    branch_id = str(workspace.primary_branch_id)
    for offset in range(1, 21):
        day = after_day + timedelta(days=offset)
        result = adapter.get_availability(AvailabilityRequest(
            branch_id=branch_id, service_id=str(service_id), booking_date=day, doctor_id=str(doctor_id)
        ))
        if result.slots:
            return day, result
    raise RuntimeError("fresh fixture could not find later same-doctor availability")


def seed_same_day_reschedule(db: Session, workspace: Workspace):
    patient, _catalog, service, doctor, _branch, day, available, tz, _slot = core.generic_context(db, workspace)
    if len(available.slots) < 2:
        raise RuntimeError("F7 requires two same-day canonical slots")
    original_slot, target_slot = available.slots[0], available.slots[1]
    row = Appointment(
        workspace_id=workspace.id, patient_id=patient.id,
        branch_id=UUID(str(original_slot.branch_id)), doctor_id=UUID(str(original_slot.doctor_id)),
        service_id=UUID(str(original_slot.service_id)), status="confirmed", source="staff",
        start_at=original_slot.start_at, end_at=original_slot.end_at,
        busy_start_at=original_slot.start_at, busy_end_at=original_slot.end_at,
        duration_minutes=original_slot.duration_minutes, price_minor=original_slot.price_minor,
        currency=original_slot.currency, payment_status="unpaid", payment_method="unknown",
        billing_context="standard", confirmed_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return patient, service, doctor, day, tz, row, target_slot


def anchor_nearest(db: Session, workspace: Workspace) -> core.PreparedScenario:
    prepared = scenarios._HANDLERS["nearest_available"](db, workspace)
    service = next((row for row in db.scalars(select(Service).where(
        Service.workspace_id == workspace.id, Service.is_active.is_(True)
    )) if row.name and row.name in prepared.messages[0]), None)
    if service is None:
        raise RuntimeError("A4 fixture service could not be resolved")
    catalog = core.build_clinic_catalog(db, workspace)
    doctors = [row for row in catalog.get("doctors", []) if isinstance(row, dict) and row.get("id")
               and str(service.id) in {str(v) for v in (row.get("service_ids") or [])}]
    if not doctors:
        raise RuntimeError("A4 fixture has no canonical service doctors")
    adapter = core.get_clinic_adapter(db=db, workspace=workspace)
    branch_id = str(workspace.primary_branch_id)
    today = datetime.now(UTC).date()
    available_days = []
    for offset in range(15):
        day = today + timedelta(days=offset)
        slots = []
        for doctor in doctors:
            result = adapter.get_availability(core.AvailabilityRequest(
                branch_id=branch_id, service_id=str(service.id), booking_date=day, doctor_id=str(doctor["id"])
            ))
            tz = ZoneInfo(result.timezone)
            slots.extend({
                "doctor_id": slot.doctor_id, "doctor_name": slot.doctor_name,
                "laser_device_key": slot.laser_device_key, "laser_device_name": slot.laser_device_name,
                "start_local": slot.start_at.astimezone(tz).isoformat(),
                "end_local": slot.end_at.astimezone(tz).isoformat(),
            } for slot in result.slots)
        windows = availability_windows_from_slots(slots)
        if windows:
            available_days.append((day, windows))
        if len(available_days) >= 2:
            break
    if len(available_days) < 2:
        raise RuntimeError("A4 requires a canonical later availability day")
    first_day, first_windows = available_days[0]
    later_day, later_windows = available_days[1]
    if len(first_windows) > PAGE_SIZE:
        raise RuntimeError("A4 first availability day must fit one presentation page")
    return core.PreparedScenario(
        patient=prepared.patient,
        messages=[prepared.messages[0], "طب اللي بعده؟"],
        observations=[*prepared.observations, "prompt03_anchor=A4",
                      f"qa_nearest_availability_date={first_day.isoformat()}",
                      f"qa_nearest_window_count={len(first_windows)}",
                      f"qa_later_availability_date={later_day.isoformat()}",
                      f"qa_later_window_count={len(later_windows)}"],
        post=prepared.post,
    )


def anchor_normal_booking(db: Session, workspace: Workspace) -> core.PreparedScenario:
    prepared = scenarios._HANDLERS["single_session_booking"](db, workspace)
    return clone(prepared, [prepared.messages[0], prepared.messages[1], "أيوه، أكد الحجز."], "prompt03_anchor=A5")


def f1(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient, _catalog, service, doctor, _branch, day, _available, tz, slot = core.generic_context(db, workspace)
    when = slot.start_at.astimezone(tz).strftime("%H:%M")
    return core.PreparedScenario(patient=patient, messages=[
        f"أنا بس بستفسر دلوقتي، لو حبيت أعمل {service['name']} مع {doctor['name']} يوم {day.isoformat()} إيه المواعيد؟ مش بحجز لسه.",
        f"خلاص قررت أحجز، ثبّتلي دلوقتي ميعاد {when}.",
    ], observations=["fresh_rule=informational_to_execute", f"target_time={when}"])


def f2(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient, _catalog, service, doctor, _branch, day1, _available, _tz, _slot = core.generic_context(db, workspace)
    day2, available2 = later_same_doctor_slot(db, workspace, service_id=service["id"], doctor_id=doctor["id"], after_day=day1)
    tz2 = ZoneInfo(available2.timezone)
    time2 = available2.slots[0].start_at.astimezone(tz2).strftime("%H:%M")
    return core.PreparedScenario(patient=patient, messages=[
        f"وريني مواعيد {service['name']} مع {doctor['name']} يوم {day1.isoformat()}، مجرد مواعيد بس.",
        f"غيّر البحث ليوم {day2.isoformat()} والساعة {time2}، وخلي الدكتور زي ما هو.",
    ], observations=["fresh_rule=multi_dimension_availability_edit", f"initial_date={day1.isoformat()}",
                     f"target_date={day2.isoformat()}", f"target_time={time2}", "expected_write_delta=0"])


def f3(db: Session, workspace: Workspace) -> core.PreparedScenario:
    prepared = scenarios._HANDLERS["nearest_available"](db, workspace)
    return clone(prepared, [
        prepared.messages[0],
        "وريني اختيارات زيادة بعد اللي ظهروا دول، أنا لسه بقارن ومش بحجز.",
        "واللي بعد دول كمان؟",
    ], "fresh_rule=next_after_continuation", "expected_write_delta=0")


def f4(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient = core.patient_without_future(db, workspace)
    _c1, service_a, doctor_a, _b1, day_a, _av1 = core.base._booking_context(db, workspace)
    _c2, service_b, doctor_b, _b2, day_b, _av2 = core.base._booking_context(db, workspace, exclude_service_id=str(service_a["id"]))
    return core.PreparedScenario(patient=patient, messages=[
        f"ابدألي حجز {service_a['name']} مع {doctor_a['name']} يوم {day_a.isoformat()} ووريني المواعيد.",
        f"بالنسبة لنفس الحجز، غيرت الخدمة: خلّيها {service_b['name']} مع {doctor_b['name']} يوم {day_b.isoformat()} بدل {service_a['name']}.",
    ], observations=["fresh_rule=explicit_service_correction_wins", f"old_service_id={service_a['id']}", f"new_service_id={service_b['id']}"])


def f5(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient = core.patient_without_future(db, workspace)
    offer = core.package_offer(db, workspace, service_name="ليزر إزالة الشعر - إبط", sessions=6, device_key="prime_lase")
    return core.PreparedScenario(patient=patient, messages=[
        "ممكن تقولي تفاصيل وسعر باكيدج 6 جلسات ليزر إزالة الشعر للإبط على Prime Lase؟",
        "وبالمناسبة، مواعيد شغل العيادة إيه؟",
        "تمام، اشتريلي دلوقتي الباكيدج اللي كنا بنتكلم عنها.",
    ], observations=["fresh_rule=package_offer_side_question_purchase",
                     "target_offer=" + json.dumps(core.jsonable(offer), ensure_ascii=False, separators=(",", ":"))])


def f6(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient, package, read = core.package_patient(db, workspace)
    service = db.get(Service, package.service_id)
    if service is None:
        raise RuntimeError("F6 package service missing")
    _catalog, doctor, day, available = core.availability(db, workspace, service_id=service.id, device_key=package.laser_device_key)
    tz = ZoneInfo(available.timezone)
    when = available.slots[0].start_at.astimezone(tz).strftime("%H:%M")
    return core.PreparedScenario(patient=patient, messages=[
        f"عندي باكيدج {service.name} شغالة، بس الحجز ده مش عايزه يتحسب منها. احجزلي جلسة عادية منفصلة يوم {day.isoformat()} مع {doctor['name']} الساعة {when}."
    ], observations=["fresh_rule=explicit_existing_package_rejection", f"existing_package_id={package.id}",
                     f"package_remaining_before={read.sessions_remaining}", f"target_time={when}"])


def f7(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient, service, doctor, day, tz, original, target = seed_same_day_reschedule(db, workspace)
    old_time = original.start_at.astimezone(tz).strftime("%H:%M")
    new_time = target.start_at.astimezone(tz).strftime("%H:%M")
    observations = ["fresh_rule=same_date_reschedule_new_time", f"original_appointment_id={original.id}",
                    f"original_date={day.isoformat()}", f"old_time={old_time}", f"new_time={new_time}"]
    def post() -> None:
        db.refresh(original)
        observations.append(f"original_status_after={original.status}")
    return core.PreparedScenario(patient=patient, messages=[
        f"ميعادي الحالي لـ{service['name']} مع {doctor['name']} يوم {day.isoformat()} الساعة {old_time}. عايز أنقله لنفس اليوم بس الساعة {new_time}."
    ], observations=observations, post=post)


def f8(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient = core.patient_without_future(db, workspace)
    original = core.base._seed_upcoming(db, workspace, patient, 1)[0]
    tz = ZoneInfo(workspace.timezone)
    local = original.start_at.astimezone(tz)
    actual = local.strftime("%H:%M")
    claimed = (local + timedelta(hours=1)).strftime("%H:%M")
    return core.PreparedScenario(patient=patient, messages=[
        "فكرني معادي الجاي إمتى والساعة كام؟",
        "على السريع، رقم تليفون العيادة إيه؟",
        f"بالنسبة لنفس المعاد اللي سألتك عنه من شوية، أنا فاكره الساعة {claimed} مش {actual}، صح؟",
    ], observations=["fresh_rule=appointment_fact_challenge_after_side_question", f"appointment_id={original.id}",
                     f"actual_time={actual}", f"claimed_time={claimed}", "expected_write_delta=0"])


def f9(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient = core.patient_without_future(db, workspace)
    prime = core.package_offer(db, workspace, service_name="ليزر إزالة الشعر - إبط", sessions=6, device_key="prime_lase")
    candela = core.package_offer(db, workspace, service_name="ليزر إزالة الشعر - إبط", sessions=6, device_key="candela_gentle")
    return core.PreparedScenario(patient=patient, messages=[
        "وريني الفرق في السعر بين باكيدج 6 جلسات إبط على Prime Lase وباكيدج 6 جلسات إبط على Candela Gentle.",
        "اختياري واضح: اشتريلي باكيدج الست جلسات بتاع Candela Gentle.",
    ], observations=["fresh_rule=multiple_offers_explicit_choice",
                     "prime_offer=" + json.dumps(core.jsonable(prime), ensure_ascii=False, separators=(",", ":")),
                     "candela_offer=" + json.dumps(core.jsonable(candela), ensure_ascii=False, separators=(",", ":")),
                     "expected_device=candela_gentle"])


def f10(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient = core.patient_without_future(db, workspace)
    offer = core.package_offer(db, workspace, service_name="ليزر إزالة الشعر - إبط", sessions=6, device_key="prime_lase")
    _catalog, doctor, day, available = core.availability(db, workspace, service_id=offer["service_id"], device_key="prime_lase")
    tz = ZoneInfo(available.timezone)
    when = available.slots[0].start_at.astimezone(tz).strftime("%H:%M")
    return core.PreparedScenario(patient=patient, messages=[
        f"اشتريلي باكيدج 6 جلسات ليزر إزالة شعر الإبط على Prime Lase، وفي نفس الطلب احجزلي أول جلسة منها يوم {day.isoformat()} مع {doctor['name']} الساعة {when}."
    ], observations=["fresh_rule=package_purchase_plus_first_booking_same_turn",
                     "target_offer=" + json.dumps(core.jsonable(offer), ensure_ascii=False, separators=(",", ":")),
                     f"target_date={day.isoformat()}", f"target_time={when}"])


def f11(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient = core.patient_without_future(db, workspace)
    _c1, service_a, doctor_a, _b1, day_a, _av1 = core.base._booking_context(db, workspace)
    _c2, service_b, doctor_b, _b2, day_b, _av2 = core.base._booking_context(db, workspace, exclude_service_id=str(service_a["id"]))
    return core.PreparedScenario(patient=patient, messages=[
        f"ابدألي حجز {service_a['name']} مع {doctor_a['name']} يوم {day_a.isoformat()} ووريني الأوقات.",
        f"سيب الطلب ده خالص. عايز أبدأ حجز جديد منفصل لـ{service_b['name']} مع {doctor_b['name']} يوم {day_b.isoformat()}، وريني مواعيده.",
    ], observations=["fresh_rule=fresh_task_after_active_booking", f"old_service_id={service_a['id']}", f"new_service_id={service_b['id']}"])


def _natural_clock(local_dt: datetime) -> tuple[int, str]:
    hour = local_dt.hour
    hour12 = hour % 12 or 12
    if hour < 12:
        period = "الصبح"
    elif hour < 18:
        period = "بعد الضهر"
    else:
        period = "بالليل"
    return hour12, period


def f12(db: Session, workspace: Workspace) -> core.PreparedScenario:
    patient = core.patient_without_future(db, workspace)
    service = core.service_by_name(db, workspace, "ليزر إزالة الشعر - إبط")
    _catalog, doctor, day, available = core.availability(db, workspace, service_id=service.id, device_key="prime_lase")
    tz = ZoneInfo(available.timezone)
    local = available.slots[0].start_at.astimezone(tz)
    hour12, period = _natural_clock(local)
    return core.PreparedScenario(patient=patient, messages=[
        f"وريني اختيارات {service.name} يوم {day.isoformat()} على Prime Lase مع أي دكتور مناسب.",
        f"أنا أقصد الاختيار بتاع دكتور {doctor['name']} اللي حوالي الساعة {hour12} {period} على Prime Lase؛ وريني الاختيار ده بس من غير حجز.",
    ], observations=["fresh_rule=new_availability_reference_language", f"target_doctor_id={doctor['id']}",
                     f"target_time={local.strftime('%H:%M')}", "target_device=prime_lase", "expected_write_delta=0"])


def install_handlers() -> None:
    scenarios._HANDLERS.update({
        "core_nearest_availability_continuation": anchor_nearest,
        "core_normal_booking": anchor_normal_booking,
        "prompt03_f1_question_to_booking": f1,
        "prompt03_f2_multi_dimension_availability_edit": f2,
        "prompt03_f3_next_after_continuation": f3,
        "prompt03_f4_service_correction_active_booking": f4,
        "prompt03_f5_package_side_question_purchase": f5,
        "prompt03_f6_reject_existing_package": f6,
        "prompt03_f7_same_date_reschedule_time": f7,
        "prompt03_f8_fact_challenge_after_side_question": f8,
        "prompt03_f9_multiple_offers_explicit_choice": f9,
        "prompt03_f10_purchase_and_book_same_turn": f10,
        "prompt03_f11_fresh_task_after_active_booking": f11,
        "prompt03_f12_new_availability_reference_language": f12,
    })
    for sid, meta in META.items():
        scenarios.TOPICS[sid] = meta["topic"]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(x) for x in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value or "")


def stage_from_messages(messages: Any) -> str:
    text = "\n".join(_content_text(getattr(message, "content", "")) for message in (messages or []))
    markers = (
        ("V2 semantic turn interpreter", "full_turn_interpreter"),
        ("narrow semantic resolver for the latest availability follow-up", "availability_reference"),
        ("presentation structure for verified clinic availability", "availability_composer"),
        ("presentation structure for completed clinic actions", "terminal_composer"),
        ("presentation structure for verified clinic pricing", "price_device_composer"),
        ("customer-facing assistant for an aesthetic clinic", "generic_responder"),
    )
    for marker, stage in markers:
        if marker in text:
            return stage
    return "unknown"


def install_stage_telemetry() -> None:
    original = core._ORIGINAL_GENERATE
    def capture(self, *args, **kwargs):
        messages = args[0] if args else kwargs.get("messages")
        stage = stage_from_messages(messages)
        result = original(self, *args, **kwargs)
        usage: dict[str, Any] = {}
        llm_output = getattr(result, "llm_output", None)
        if isinstance(llm_output, dict) and isinstance(llm_output.get("token_usage"), dict):
            usage = dict(llm_output["token_usage"])
        if not usage:
            try:
                raw = getattr(result.generations[0].message, "usage_metadata", None)
                if isinstance(raw, dict):
                    usage = dict(raw)
            except Exception:
                pass
        def pick(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)
                if isinstance(value, int):
                    return value
            return 0
        input_tokens = pick("prompt_tokens", "input_tokens", "input_token_count")
        output_tokens = pick("completion_tokens", "output_tokens", "output_token_count")
        total_tokens = pick("total_tokens", "total_token_count") or input_tokens + output_tokens
        model_name = getattr(self, "model_name", None) or getattr(self, "model", None)
        core._TOKEN_BUCKET.append({
            "stage": stage, "model": str(model_name) if model_name else None,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
        })
        return result
    ChatOpenAI._generate = capture


def fixture_preflight() -> dict[str, Any]:
    install_handlers()
    verify_output_path()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    rows = []
    try:
        with engine.connect() as bootstrap_connection:
            bootstrap_connection.exec_driver_sql("select 1")
        for sid in ORDER:
            connection = engine.connect()
            outer = connection.begin()
            db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                workspace = db.scalar(select(Workspace).where(Workspace.slug == core.WORKSPACE_SLUG))
                if workspace is None:
                    raise RuntimeError("Workspace tia not found")
                prepared = scenarios.prepare_scenario(db, workspace, sid)
                expected = META[sid]["planned_turns"]
                if len(prepared.messages) != expected:
                    raise RuntimeError(f"{sid}: expected {expected} messages, got {len(prepared.messages)}")
                rows.append({"scenario": sid, "label": META[sid]["label"], "messages": list(prepared.messages),
                             "observations": list(prepared.observations), "status": "READY"})
            finally:
                db.close()
                if outer.is_active:
                    outer.rollback()
                connection.close()
    finally:
        engine.dispose()
    if _PREFLIGHT_PROVIDER_CALLS != 0:
        raise RuntimeError(f"Provider calls occurred during fixture preflight: {_PREFLIGHT_PROVIDER_CALLS}")
    return {
        "head": git_head(),
        "scenario_count": len(rows),
        "anchor_count": len(ANCHORS),
        "fresh_count": len(FRESH),
        "turns": sum(META[x]["planned_turns"] for x in ORDER),
        "provider_calls": _PREFLIGHT_PROVIDER_CALLS,
        "database_bootstrap": "PASS",
        "scenario_reset_capability": "PASS",
        "transaction_rollbacks": len(rows),
        "database_writes_persisted": False,
        "evidence_output_path": str(REPORT),
        "scenarios": rows,
    }


def percentile(values: list[int], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def quality_flags(reply: str | None) -> dict[str, bool]:
    text = str(reply or "")
    lowered = text.casefold()
    return {
        "empty": not bool(text.strip()),
        "uuid_leak": bool(re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", text, re.I)),
        "internal_marker_leak": any(x in lowered for x in ("option_ref", "response_goal", "write_ready", "structured_understanding")),
        "error_text": any(x in lowered for x in ("traceback", "internal server error", "structuredoutputerror")),
    }


def run_hybrid() -> int:
    if git_head() != TARGET_HEAD:
        raise RuntimeError(f"Exact-head guard failed: {git_head()} != {TARGET_HEAD}")
    install_handlers()
    install_stage_telemetry()
    started = datetime.now(UTC)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results = []
    try:
        for index, sid in enumerate(ORDER, 1):
            print(f"[{index:02d}/{len(ORDER)}] {sid}", flush=True)
            result = core.execute_one(engine, sid, META[sid]["topic"])
            results.append(result)
            print("PROMPT03_SCENARIO=" + json.dumps({
                "scenario": sid, "label": META[sid]["label"], "error": result.error,
                "turns": len(result.turns), "db_delta": result.db_delta,
            }, ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        engine.dispose()

    conversations = []
    all_calls = []
    durations = []
    for result in results:
        item = asdict(result)
        sid = result.name
        item["portfolio"] = META[sid]
        for i, turn in enumerate(item["turns"], 1):
            turn["turn_number"] = i
            turn["quality_flags"] = quality_flags(turn.get("assistant"))
            for call in (turn.get("token_usage") or {}).get("calls", []):
                all_calls.append({"scenario": sid, "turn": i, **call})
            durations.append(int(turn.get("duration_ms") or 0))
        conversations.append(item)

    by_stage = {}
    for call in all_calls:
        stage = str(call.get("stage") or "unknown")
        bucket = by_stage.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                             "total_tokens": 0, "models": Counter()})
        bucket["calls"] += 1
        bucket["input_tokens"] += int(call.get("input_tokens") or 0)
        bucket["output_tokens"] += int(call.get("output_tokens") or 0)
        bucket["total_tokens"] += int(call.get("total_tokens") or 0)
        bucket["models"][str(call.get("model"))] += 1
    for bucket in by_stage.values():
        bucket["models"] = dict(bucket["models"])

    same_stage_repeats = []
    for item in conversations:
        for turn in item["turns"]:
            counts = Counter(str(c.get("stage") or "unknown") for c in (turn.get("token_usage") or {}).get("calls", []))
            for stage, count in counts.items():
                if count > 1:
                    same_stage_repeats.append({"scenario": item["name"], "turn": turn["turn_number"], "stage": stage, "count": count})
    fallback_calls = [x for x in all_calls if str(x.get("model") or "").casefold() == "gpt-5-mini"]
    anchor_inputs = sum(int(t.get("token_usage", {}).get("input_tokens") or 0)
                        for c in conversations if c["portfolio"]["population"] == "anchor" for t in c["turns"])
    fresh_inputs = sum(int(t.get("token_usage", {}).get("input_tokens") or 0)
                       for c in conversations if c["portfolio"]["population"] == "fresh" for t in c["turns"])
    payload = {
        "audit_metadata": {
            "audit_name": "SENIOR-PROMPT-03 HYBRID CORE", "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(), "head": git_head(),
            "approved_base": "2f33339f882291f59e665b9a2d9ddff16500e27a", "approved_head": TARGET_HEAD,
            "model_configuration": {"provider": settings.llm_provider, "primary_model": settings.openai_model,
                "fallback_model": settings.openai_fallback_model, "reasoning_effort": settings.openai_reasoning_effort,
                "fallback_reasoning_effort": settings.openai_fallback_reasoning_effort},
            "database_writes_persisted": False, "c7": "OUT_OF_SCOPE", "phase_3_3": "NOT_RUN",
        },
        "inventory": {"conversation_count": len(PORTFOLIO), "anchor_count": len(ANCHORS), "fresh_count": len(FRESH),
                      "planned_turns": sum(row[2] for row in PORTFOLIO),
                      "scenarios": [{"id": row[0], **META[row[0]]} for row in PORTFOLIO]},
        "conversations": conversations,
        "telemetry": {"generations": len(all_calls),
                      "input_tokens": sum(int(x.get("input_tokens") or 0) for x in all_calls),
                      "output_tokens": sum(int(x.get("output_tokens") or 0) for x in all_calls),
                      "total_tokens": sum(int(x.get("total_tokens") or 0) for x in all_calls),
                      "anchor_input_tokens": anchor_inputs, "fresh_input_tokens": fresh_inputs,
                      "by_stage": by_stage, "same_stage_repeats": same_stage_repeats,
                      "fallback_calls": fallback_calls,
                      "unknown_stage_calls": [x for x in all_calls if x.get("stage") == "unknown"]},
        "latency": {"turns": len(durations), "p50_ms": percentile(durations, .50),
                    "p90_ms": percentile(durations, .90), "p95_ms": percentile(durations, .95),
                    "max_ms": max(durations) if durations else None,
                    "over_15s": sum(x > 15000 for x in durations), "over_20s": sum(x > 20000 for x in durations)},
        "runtime_errors": [{"scenario": x.name, "error": x.error} for x in results if x.error],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("PROMPT03_SUMMARY=" + json.dumps({"conversations": len(conversations),
        "turns": sum(len(x["turns"]) for x in conversations), "generations": len(all_calls),
        "runtime_errors": len(payload["runtime_errors"]), "input_tokens": payload["telemetry"]["input_tokens"],
        "output_tokens": payload["telemetry"]["output_tokens"], "fallback_calls": len(fallback_calls),
        "same_stage_repeats": len(same_stage_repeats)}, separators=(",", ":")), flush=True)
    print(f"REPORT={REPORT}", flush=True)
    return 1 if payload["runtime_errors"] else 0


def inventory_only() -> int:
    if git_head() != TARGET_HEAD:
        raise RuntimeError(f"Exact-head guard failed: {git_head()} != {TARGET_HEAD}")
    install_provider_forbidden_guard()
    payload = {
        "target_head": TARGET_HEAD,
        "tested_head": git_head(),
        "conversation_count": len(PORTFOLIO),
        "anchor_count": len(ANCHORS),
        "fresh_count": len(FRESH),
        "planned_turns": sum(row[2] for row in PORTFOLIO),
        "provider_calls": _PREFLIGHT_PROVIDER_CALLS,
        "scenarios": [{"id": row[0], **META[row[0]]} for row in PORTFOLIO],
    }
    if payload["conversation_count"] != 17 or payload["anchor_count"] != 5 or payload["fresh_count"] != 12:
        raise RuntimeError(f"Hybrid inventory count mismatch: {payload}")
    if payload["planned_turns"] != 35:
        raise RuntimeError(f"Hybrid planned-turn mismatch: {payload['planned_turns']}")
    if payload["provider_calls"] != 0:
        raise RuntimeError(f"Provider calls occurred during inventory preflight: {payload['provider_calls']}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--fixture-preflight", action="store_true")
    args = parser.parse_args()
    if args.inventory_only:
        return inventory_only()
    if args.fixture_preflight:
        if git_head() != TARGET_HEAD:
            raise RuntimeError(f"Exact-head guard failed: {git_head()} != {TARGET_HEAD}")
        install_provider_forbidden_guard()
        print(json.dumps(fixture_preflight(), ensure_ascii=False, indent=2, default=str))
        return 0
    return run_hybrid()


if __name__ == "__main__":
    raise SystemExit(main())
