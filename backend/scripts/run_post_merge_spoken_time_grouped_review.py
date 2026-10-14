from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.appointment import Appointment
from scripts import run_explicit_daypart_live_review as base
from scripts import run_clinic_hours_clock_live_review as clock
from scripts.run_grouped_multiservice_intent_live_review import rollback_session

TARGET_SHA = os.environ.get("POST_MERGE_TARGET_SHA", "").strip()
EXPECTED_TARGET_SHA = "20c61ce1cc699aa44f8c3c6b80b66abfb03e203e"
REPORT_PATH = Path(os.environ.get(
    "POST_MERGE_REVIEW_REPORT_PATH",
    "artifacts/post-merge-spoken-time-grouped-review.json",
))

COUNTERS = {
    "wrong_writes": 0,
    "partial_writes": 0,
    "false_success": 0,
    "wrong_time_writes": 0,
    "silent_inferred_period_authority": 0,
    "fabricated_period_evidence_accepted": 0,
    "unnecessary_am_pm_clarifications": 0,
    "arbitrary_10_11_resolution": 0,
    "lost_services": 0,
    "stale_retained_services": 0,
    "service_set_integrity_failures": 0,
    "wrong_device": 0,
    "cross_device_visits": 0,
    "duplicate_appointments": 0,
    "duplicate_proposals": 0,
    "stale_proposal_reuse": 0,
    "stale_option_reuse": 0,
    "persisted_audit_writes": 0,
}

RESULTS: list[dict[str, Any]] = []
STOP: dict[str, Any] | None = None


def require(condition: bool, *, scenario: str, counter: str, message: str) -> None:
    global STOP
    if condition:
        return
    COUNTERS[counter] += 1
    STOP = {
        "scenario": scenario,
        "counter": counter,
        "message": message,
        "classification": "UNCLASSIFIED_PENDING_EVIDENCE_REVIEW",
    }
    raise AssertionError(f"{scenario}: {message}")


def task_constraints(record: dict[str, Any]) -> dict[str, Any]:
    task = record.get("active_task")
    if not isinstance(task, dict):
        return {}
    constraints = task.get("constraints")
    return constraints if isinstance(constraints, dict) else {}


def service_ids(record: dict[str, Any]) -> set[str]:
    return set(base._task_service_ids(record))


def proposal(record: dict[str, Any]) -> dict[str, Any] | None:
    return base._proposal(record) or (
        record.get("booking_proposal")
        if isinstance(record.get("booking_proposal"), dict)
        else None
    )


def proposal_services(record: dict[str, Any]) -> set[str]:
    value = proposal(record)
    if not isinstance(value, dict):
        return set()
    raw = value.get("service_ids")
    return {str(item) for item in raw} if isinstance(raw, list) else set()


def option_fingerprint(record: dict[str, Any]) -> set[str]:
    return {
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        for item in base._reference_options(record)
    }


def period_question(reply: str) -> bool:
    lowered = reply.lower()
    morning = any(x in lowered for x in ("الصبح", "صباح", "صباحًا"))
    night = any(x in lowered for x in ("بالليل", "ليل", "مساء", "مساءً"))
    return morning and night


def semantic_time(record: dict[str, Any]) -> dict[str, Any]:
    values = base._raw_semantic_times(record)
    if not values:
        return {}
    raw = values[-1].get("time")
    return raw if isinstance(raw, dict) else {}


def period_evidence(record: dict[str, Any]) -> str | None:
    raw = semantic_time(record).get("period_evidence_text")
    if raw in (None, ""):
        return None
    return str(raw).strip() or None


def evidence_verified(record: dict[str, Any]) -> bool:
    evidence = period_evidence(record)
    if evidence is None:
        return True
    customer = str(record.get("customer") or "")
    return evidence in customer and not any(ch.isdigit() for ch in evidence) and ":" not in evidence


def availability_clock(record: dict[str, Any]) -> str | None:
    return clock._availability_clock(record)


def proposal_clock(record: dict[str, Any]) -> str | None:
    return clock._proposal_clock(record)


def canonical_clock(record: dict[str, Any]) -> str | None:
    for value in (
        availability_clock(record),
        base._task_time(record),
        proposal_clock(record),
        base._appointment_clock(record),
    ):
        if value:
            return value
    raw = semantic_time(record).get("start_time")
    return str(raw) if raw else None


def appointment_delta(record: dict[str, Any]) -> int:
    return int((record.get("db_effect") or {}).get("appointment_delta") or 0)


def ensure_common_safety(ctx: base.Context, record: dict[str, Any], scenario: str) -> None:
    appointments = record.get("appointments") or []
    require(len(appointments) <= 1, scenario=scenario, counter="duplicate_appointments", message="more than one patient appointment exists")
    require(len(record.get("proposals_created") or []) <= 1, scenario=scenario, counter="duplicate_proposals", message="more than one proposal created in one turn")
    require(evidence_verified(record), scenario=scenario, counter="fabricated_period_evidence_accepted", message="provider period evidence is not grounded in latest customer text")
    reply = str(record.get("agent") or "").strip()
    require(bool(reply), scenario=scenario, counter="false_success", message="empty Agent reply")
    for appointment in appointments:
        require(str(appointment.get("doctor_id") or "") == str(ctx.doctor.id), scenario=scenario, counter="wrong_writes", message="appointment used wrong doctor")
        keys = {
            str(value)
            for value in [
                appointment.get("laser_device_key"),
                *[
                    line.get("laser_device_key")
                    for line in appointment.get("additional_services") or []
                    if isinstance(line, dict)
                ],
            ]
            if value not in (None, "")
        }
        require(len(keys) <= 1, scenario=scenario, counter="cross_device_visits", message="same visit used multiple device keys")
        require(not keys or keys == {"candela_gentle"}, scenario=scenario, counter="wrong_device", message=f"unexpected device keys {sorted(keys)}")


def clock_trace() -> dict[str, Any] | None:
    return clock._original_bare_trace()


def record_evidence(scenario: str, ctx: base.Context, record: dict[str, Any], *, expected: str | None, clarification: bool) -> dict[str, Any]:
    trace = clock_trace()
    constraints = task_constraints(record)
    task_time = constraints.get("time") if isinstance(constraints.get("time"), dict) else {}
    return {
        "customer": record.get("customer"),
        "agent": record.get("agent"),
        "raw_semantic_time": semantic_time(record),
        "period_evidence_text": period_evidence(record),
        "period_evidence_verified": evidence_verified(record),
        "backend_start_time_ambiguity": (trace.get("input_time") or {}).get("start_time_ambiguity") if isinstance(trace, dict) else task_time.get("start_time_ambiguity"),
        "clock_source": constraints.get("time_clock_source"),
        "clinic_hours_used": trace.get("operating_hours") if isinstance(trace, dict) else None,
        "candidate_clocks": trace.get("candidate_clocks") if isinstance(trace, dict) else ([expected] if expected else []),
        "valid_operating_hour_candidates": trace.get("valid_operating_hour_candidates") if isinstance(trace, dict) else None,
        "canonical_time": canonical_clock(record),
        "availability_time": availability_clock(record),
        "proposal_time": proposal_clock(record),
        "appointment_time": base._appointment_clock(record),
        "am_pm_clarification": period_question(str(record.get("agent") or "")),
        "appointment_delta": appointment_delta(record),
        "expected": expected,
        "clarification_expected": clarification,
    }


def chat(ctx: base.Context, customer: str) -> dict[str, Any]:
    clock.CLOCK_TRACE.clear()
    return base._chat(ctx, customer)


def set_hours(ctx: base.Context, start: str, end: str) -> None:
    clock._set_operating_hours(ctx, start=start, end=end)


def persisted_appointment_count(workspace_id: UUID) -> int:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            return int(db.scalar(select(func.count()).select_from(Appointment).where(Appointment.workspace_id == workspace_id)) or 0)
    finally:
        engine.dispose()


def run_isolated(name: str, runner) -> None:
    workspace_id: UUID | None = None
    result: dict[str, Any] = {"scenario": name, "status": "RUNNING"}
    try:
        with rollback_session() as db:
            ctx = base._setup(db, name)
            workspace_id = ctx.workspace.id
            result["details"] = runner(ctx)
            result["status"] = "PASS"
        if workspace_id is not None:
            persisted = persisted_appointment_count(workspace_id)
            require(persisted == 0, scenario=name, counter="persisted_audit_writes", message=f"rollback leaked {persisted} appointment(s)")
        result["persisted_audit_writes"] = 0
    except Exception as exc:
        result["status"] = "FAIL"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        RESULTS.append(result)
        write_report()
        raise
    RESULTS.append(result)


def validate_time_turn(ctx: base.Context, scenario: str, record: dict[str, Any], *, expected: str | None, explicit: bool = False, clarification: bool = False, expected_candidates: tuple[str, ...] | None = None, expected_valid: tuple[str, ...] | None = None) -> dict[str, Any]:
    ensure_common_safety(ctx, record, scenario)
    evidence = period_evidence(record)
    if explicit:
        require(evidence is not None and evidence_verified(record), scenario=scenario, counter="fabricated_period_evidence_accepted", message="explicit daypart lacks grounded customer evidence")
    else:
        require(evidence is None, scenario=scenario, counter="silent_inferred_period_authority", message=f"bare/literal clock gained provider period authority: {evidence!r}")
    if clarification:
        require(period_question(str(record.get("agent") or "")), scenario=scenario, counter="arbitrary_10_11_resolution", message="both 10/11 candidates valid but Agent did not ask morning/night")
        require(not record.get("availability_requests") and proposal(record) is None, scenario=scenario, counter="arbitrary_10_11_resolution", message="unresolved ambiguous clock progressed to availability/proposal")
        require(appointment_delta(record) == 0 and not record.get("appointments"), scenario=scenario, counter="wrong_writes", message="unresolved ambiguous clock wrote an appointment")
    else:
        require(not period_question(str(record.get("agent") or "")), scenario=scenario, counter="unnecessary_am_pm_clarifications", message="Agent asked AM/PM when policy says not to")
        require(canonical_clock(record) == expected, scenario=scenario, counter="wrong_time_writes", message=f"canonical time {canonical_clock(record)!r} != {expected!r}")
        if record.get("appointments"):
            require(base._appointment_clock(record) == expected, scenario=scenario, counter="wrong_time_writes", message="appointment time differs from canonical requested time")
    trace = clock_trace()
    if expected_candidates is not None:
        require(isinstance(trace, dict) and tuple(trace.get("candidate_clocks") or []) == expected_candidates, scenario=scenario, counter="silent_inferred_period_authority", message=f"backend candidate clocks mismatch: {trace}")
    if expected_valid is not None:
        require(isinstance(trace, dict) and tuple(trace.get("valid_operating_hour_candidates") or []) == expected_valid, scenario=scenario, counter=("arbitrary_10_11_resolution" if clarification else "wrong_time_writes"), message=f"backend valid operating candidates mismatch: {trace}")
    return record_evidence(scenario, ctx, record, expected=expected, clarification=clarification)


def single_time(scenario: str, customer: str, hours: tuple[str, str], expected: str | None, *, explicit: bool = False, clarification: bool = False, candidates: tuple[str, ...] | None = None, valid: tuple[str, ...] | None = None):
    def runner(ctx: base.Context):
        set_hours(ctx, *hours)
        record = chat(ctx, customer)
        return validate_time_turn(ctx, scenario, record, expected=expected, explicit=explicit, clarification=clarification, expected_candidates=candidates, expected_valid=valid)
    run_isolated(scenario, runner)


def setup_single_booking(ctx: base.Context) -> dict[str, Any]:
    return chat(ctx, "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026، بس وريني التفاصيل قبل ما تأكد")


def pm14(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "10:00", "22:00")
    setup = setup_single_booking(ctx)
    first = chat(ctx, "خليها الساعة 3")
    first_ev = validate_time_turn(ctx, "PM14", first, expected="15:00")
    old_proposal = proposal(first)
    old_options = option_fingerprint(first)
    corrected = chat(ctx, "لا معلش خليها 4 ونص")
    second_ev = validate_time_turn(ctx, "PM14", corrected, expected="16:30")
    require(base._task_time(corrected) == "16:30", scenario="PM14", counter="wrong_time_writes", message="correction did not replace canonical task time")
    new_proposal = proposal(corrected)
    if old_proposal is not None and new_proposal is not None:
        require(proposal_clock(new_proposal) == "16:30", scenario="PM14", counter="stale_proposal_reuse", message="old 15:00 proposal survived 16:30 correction")
    new_options = option_fingerprint(corrected)
    if old_options and new_options:
        require(old_options != new_options, scenario="PM14", counter="stale_option_reuse", message="old availability option snapshot reused after time correction")
    require(len(corrected.get("appointments") or []) <= 1, scenario="PM14", counter="duplicate_appointments", message="time correction created duplicate appointment")
    return {"setup": setup, "turn_1": first_ev, "turn_2": second_ev}


def pm15(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "00:00", "22:00")
    setup = setup_single_booking(ctx)
    first = chat(ctx, "خليها الساعة 3")
    first_ev = validate_time_turn(ctx, "PM15", first, expected="15:00")
    corrected = chat(ctx, "قصدي 3 الصبح")
    second_ev = validate_time_turn(ctx, "PM15", corrected, expected="03:00", explicit=True)
    require(base._task_time(corrected) == "03:00", scenario="PM15", counter="wrong_time_writes", message="explicit morning correction did not win")
    return {"setup": setup, "turn_1": first_ev, "turn_2": second_ev}


def pm16(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "09:00", "23:30")
    setup = setup_single_booking(ctx)
    ambiguous = chat(ctx, "ممكن الساعة 10؟")
    first_ev = validate_time_turn(ctx, "PM16", ambiguous, expected=None, clarification=True, expected_candidates=("10:00", "22:00"), expected_valid=("10:00", "22:00"))
    before_constraints = task_constraints(ambiguous)
    side = chat(ctx, "طب الجلسة مدتها قد إيه؟")
    ensure_common_safety(ctx, side, "PM16")
    side_constraints = task_constraints(side)
    require(side_constraints.get("service_id") == before_constraints.get("service_id") and side_constraints.get("date") == before_constraints.get("date") and side_constraints.get("time") == before_constraints.get("time"), scenario="PM16", counter="partial_writes", message="side question lost or mutated pending 10/22 booking constraints")
    require(appointment_delta(side) == 0, scenario="PM16", counter="wrong_writes", message="side question performed a booking write")
    final = chat(ctx, "بالليل")
    final_ev = validate_time_turn(ctx, "PM16", final, expected="22:00", explicit=True)
    require(base._task_time(final) == "22:00" or base._appointment_clock(final) == "22:00", scenario="PM16", counter="wrong_time_writes", message="night continuation did not resolve pending 10 to 22:00")
    return {"setup": setup, "ambiguous": first_ev, "side_question": record_evidence("PM16", ctx, side, expected=None, clarification=False), "night_continuation": final_ev}


def grouped_start(ctx: base.Context, *, clock_text: str = "4 ونص", preview: bool = True):
    suffix = "، بس وريني التفاصيل قبل ما تأكد" if preview else ""
    return chat(ctx, f"عايزة ليزر إبط وبكيني مع بعض يوم الخميس 15 أكتوبر 2026 الساعة {clock_text}{suffix}")


def assert_grouped_record(ctx: base.Context, scenario: str, record: dict[str, Any], expected: set[str], *, absent: set[str] | None = None) -> None:
    ensure_common_safety(ctx, record, scenario)
    actual = service_ids(record)
    require(actual == expected, scenario=scenario, counter="service_set_integrity_failures", message=f"requested service set {sorted(actual)} != {sorted(expected)}")
    if absent:
        require(actual.isdisjoint(absent), scenario=scenario, counter="stale_retained_services", message=f"replaced/removed service survived: {sorted(actual & absent)}")
    prop_services = proposal_services(record)
    if prop_services:
        require(prop_services == expected, scenario=scenario, counter="stale_proposal_reuse", message=f"proposal service set is stale: {sorted(prop_services)}")


def pg1(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "10:00", "22:00")
    start = grouped_start(ctx)
    expected_start = {str(ctx.underarm.id), str(ctx.bikini.id)}
    assert_grouped_record(ctx, "PG1", start, expected_start)
    old_options = option_fingerprint(start)
    corrected = chat(ctx, "بدل ليزر بكيني خليها Full Arms وخلي ليزر الإبط زي ما هو")
    expected = {str(ctx.underarm.id), str(ctx.arms.id)}
    assert_grouped_record(ctx, "PG1", corrected, expected, absent={str(ctx.bikini.id)})
    new_options = option_fingerprint(corrected)
    if old_options and new_options:
        require(old_options != new_options, scenario="PG1", counter="stale_option_reuse", message="pre-replacement options reused")
    return {"start": start, "replacement": corrected}


def pg2(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "10:00", "22:00")
    start = grouped_start(ctx)
    corrected = chat(ctx, "ضيف عليهم Full Arms")
    expected = {str(ctx.underarm.id), str(ctx.bikini.id), str(ctx.arms.id)}
    assert_grouped_record(ctx, "PG2", corrected, expected)
    return {"start": start, "addition": corrected}


def assert_single_grouped_appointment(ctx: base.Context, scenario: str, record: dict[str, Any], expected: set[str], expected_clock: str) -> None:
    ensure_common_safety(ctx, record, scenario)
    appointments = record.get("appointments") or []
    require(len(appointments) == 1, scenario=scenario, counter="duplicate_appointments", message=f"expected exactly one appointment, found {len(appointments)}")
    actual_services = base._appointment_service_ids(record)
    require(actual_services == expected, scenario=scenario, counter="lost_services", message=f"appointment services {sorted(actual_services)} != {sorted(expected)}")
    require(base._appointment_clock(record) == expected_clock, scenario=scenario, counter="wrong_time_writes", message="appointment used wrong time")


def pg3(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "10:00", "22:00")
    start = grouped_start(ctx)
    replacement = chat(ctx, "بدل ليزر بكيني خليها Full Arms وخلي ليزر الإبط زي ما هو")
    expected = {str(ctx.underarm.id), str(ctx.arms.id)}
    assert_grouped_record(ctx, "PG3", replacement, expected, absent={str(ctx.bikini.id)})
    require(proposal(replacement) is not None, scenario="PG3", counter="false_success", message="replacement did not present a grouped proposal before confirmation")
    require(not replacement.get("appointments"), scenario="PG3", counter="wrong_writes", message="replacement preview wrote before confirmation")
    confirmed = chat(ctx, "تمام أكد الحجز")
    assert_single_grouped_appointment(ctx, "PG3", confirmed, expected, "16:30")
    require(appointment_delta(confirmed) == 1, scenario="PG3", counter="partial_writes", message="confirmation did not create exactly one appointment")
    return {"start": start, "replacement": replacement, "confirmation": confirmed}


def pg4(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "10:00", "22:00")
    preview = grouped_start(ctx)
    expected = {str(ctx.underarm.id), str(ctx.bikini.id)}
    assert_grouped_record(ctx, "PG4", preview, expected)
    require(canonical_clock(preview) == "16:30", scenario="PG4", counter="wrong_time_writes", message="grouped preview did not resolve 4:30 to 16:30")
    require(proposal(preview) is not None, scenario="PG4", counter="false_success", message="informational preview did not create one grouped proposal")
    require(appointment_delta(preview) == 0 and not preview.get("appointments"), scenario="PG4", counter="wrong_writes", message="informational preview wrote before confirmation")
    confirmed = chat(ctx, "تمام أكد")
    assert_single_grouped_appointment(ctx, "PG4", confirmed, expected, "16:30")
    require(appointment_delta(confirmed) == 1, scenario="PG4", counter="partial_writes", message="confirmation appointment_delta != 1")
    return {"preview": preview, "confirmation": confirmed}


def px1(ctx: base.Context) -> dict[str, Any]:
    set_hours(ctx, "10:00", "22:00")
    first = grouped_start(ctx, clock_text="3", preview=False)
    initial = {str(ctx.underarm.id), str(ctx.bikini.id)}
    assert_grouped_record(ctx, "PX1", first, initial)
    require(canonical_clock(first) == "15:00", scenario="PX1", counter="wrong_time_writes", message="bare 3 did not resolve to 15:00")
    require(period_evidence(first) is None, scenario="PX1", counter="silent_inferred_period_authority", message="bare 3 gained explicit provider period evidence")
    require(not period_question(str(first.get("agent") or "")), scenario="PX1", counter="unnecessary_am_pm_clarifications", message="bare 3 asked AM/PM")
    corrected = chat(ctx, "خلي البكيني Full Arms بدلها وخلي الإبط زي ما هو")
    expected = {str(ctx.underarm.id), str(ctx.arms.id)}
    assert_grouped_record(ctx, "PX1", corrected, expected, absent={str(ctx.bikini.id)})
    require(canonical_clock(corrected) == "15:00", scenario="PX1", counter="wrong_time_writes", message="service correction lost the 15:00 time")
    require(len(corrected.get("appointments") or []) <= 1, scenario="PX1", counter="duplicate_appointments", message="service correction produced duplicate appointment")
    confirmed = chat(ctx, "تمام أكد")
    assert_single_grouped_appointment(ctx, "PX1", confirmed, expected, "15:00")
    return {"initial": first, "replacement": corrected, "confirmation": confirmed}


def write_report() -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "workflow": "Post-Merge Spoken-Time & Grouped Regression Review",
        "target_sha": TARGET_SHA,
        "expected_target_sha": EXPECTED_TARGET_SHA,
        "status": "FAIL" if STOP else ("PASS" if len(RESULTS) == 22 and all(x.get("status") == "PASS" for x in RESULTS) else "INCOMPLETE"),
        "results": RESULTS,
        "safety_counters": COUNTERS,
        "stop": STOP,
    }
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("POST_MERGE_REVIEW_SUMMARY=" + json.dumps({"status": payload["status"], "target_sha": TARGET_SHA, "scenarios_completed": len(RESULTS), "safety_counters": COUNTERS, "stop": STOP}, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> int:
    require(TARGET_SHA == EXPECTED_TARGET_SHA, scenario="TARGET-SHA", counter="wrong_writes", message=f"workflow target {TARGET_SHA!r} != exact merged main {EXPECTED_TARGET_SHA}")

    single_time("PM1", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3", ("10:00", "22:00"), "15:00", candidates=("03:00", "15:00"), valid=("15:00",))
    single_time("PM2", "ممكن أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4 وربع؟", ("10:00", "22:00"), "16:15", candidates=("04:15", "16:15"), valid=("16:15",))
    single_time("PM3", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3", ("00:00", "06:00"), "03:00", candidates=("03:00", "15:00"), valid=("03:00",))
    single_time("PM4", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026، ممكن الساعة 10؟", ("09:00", "23:30"), None, clarification=True, candidates=("10:00", "22:00"), valid=("10:00", "22:00"))
    single_time("PM5", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 10", ("09:00", "21:00"), "10:00", candidates=("10:00", "22:00"), valid=("10:00",))
    single_time("PM6", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 10", ("14:00", "23:30"), "22:00", candidates=("10:00", "22:00"), valid=("22:00",))
    single_time("PM7", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11", ("09:00", "23:59"), None, clarification=True, candidates=("11:00", "23:00"), valid=("11:00", "23:00"))
    single_time("PM8", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 12", ("10:00", "22:00"), "12:00", candidates=("12:00",), valid=("12:00",))
    single_time("PM9", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3 الصبح", ("00:00", "06:00"), "03:00", explicit=True)
    single_time("PM10", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3 العصر", ("10:00", "22:00"), "15:00", explicit=True)
    single_time("PM11", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11 بالليل", ("18:00", "23:59"), "23:00", explicit=True)
    single_time("PM12", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 12 بالليل", ("00:00", "06:00"), "00:00", explicit=True)
    single_time("PM13", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 12 الظهر", ("10:00", "22:00"), "12:00", explicit=True)

    run_isolated("PM14", pm14)
    run_isolated("PM15", pm15)
    run_isolated("PM16", pm16)
    run_isolated("PG1", pg1)
    run_isolated("PG2", pg2)
    run_isolated("PG3", pg3)
    run_isolated("PG4", pg4)
    run_isolated("PX1", px1)

    single_time("DEFENSIVE-03:00", "عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 03:00", ("00:00", "06:00"), "03:00", explicit=False)

    require(all(value == 0 for value in COUNTERS.values()), scenario="SAFETY", counter="wrong_writes", message=f"non-zero safety counters: {COUNTERS}")
    write_report()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        write_report()
        raise
