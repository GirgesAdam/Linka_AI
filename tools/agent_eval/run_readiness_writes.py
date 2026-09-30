from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.agents.clinic_grounding import build_clinic_catalog

from tools.agent_eval.harness import local_slot, send_turn, service_by_slug
from tools.agent_eval.readiness_common import named_patient, new_patient, run_group
from tools.agent_eval.run_batch_01 import created_appointments, doctor_name
from tools.agent_eval.run_batch_02 import laser_context
from tools.agent_eval.run_batch_03 import (
    _appointment_by_id,
    _run_messages,
    _seed_future_appointment,
    db_delta,
    extended_state_snapshot,
    make_result,
)
from tools.agent_eval.run_batch_04 import (
    _availability_for,
    _doctor_row,
    _replacement_rows,
)


def _messages(db, ws, patient, sid, messages):
    return _run_messages(db, ws, patient, sid, messages)[0]


def _seed_one(db, ws, patient, service_slug="hydrafacial"):
    service = service_by_slug(db, ws, service_slug)
    available, slot = _availability_for(db, ws, service=service)
    row = _seed_future_appointment(
        db, ws, patient, service=service,
        doctor_id=UUID(str(slot.doctor_id)), slot=slot,
    )
    return service, available, slot, row


def case_21_simple_booking(db, ws):
    patient = new_patient(db, ws, first="نور")
    service = service_by_slug(db, ws, "hydrafacial")
    available, slot = _availability_for(db, ws, service=service)
    doctor = _doctor_row(build_clinic_catalog(db, ws), slot.doctor_id)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w21_simple_booking", [
        f"لو سمحتي احجزيلي هيدرافيشل يوم {day} الساعة {time_text} مع {doctor_name(doctor)}"
    ])
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["start_at"] == slot.start_at.isoformat()
    return make_result(
        scenario_id="w21_simple_booking", category="booking",
        purpose="Create exactly one appointment for the requested verified slot.",
        turns=turns, before=before, after=after,
        verification={"created": created, "expected_start_at": slot.start_at.isoformat()},
        deterministic_ok=ok, expected="Exactly one matching booking is created.",
        issue_title="Simple booking wrote the wrong appointment state",
    )


def case_22_ambiguous_booking(db, ws):
    patient = new_patient(db, ws, first="منة")
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w22_ambiguous_booking", [
        "ممكن تحجزيلي PRP الأسبوع الجاي؟",
        "للبشرة مش الشعر",
    ])
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    ok = not created
    return make_result(
        scenario_id="w22_ambiguous_booking", category="booking",
        purpose="Clarify ambiguous PRP and retain the verified choice across turns.",
        turns=turns, before=before, after=after,
        verification={"created": created}, deterministic_ok=ok,
        expected="No booking before date/time is resolved.",
        issue_title="Ambiguous booking wrote before required clarification",
    )


def case_23_laser_device_booking(db, ws):
    patient = new_patient(db, ws, first="يارا")
    _catalog, service, doctor, available, slot = laser_context(
        db, ws, service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w23_laser_device_booking", [
        f"عايزة احجز ليزر إبط يوم {day} الساعة {time_text} مع {doctor_name(doctor)}",
        "خليه على Candela Gentle",
    ])
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["laser_device_key"] == "candela_gentle"
    return make_result(
        scenario_id="w23_laser_device_booking", category="booking",
        purpose="Resolve device selection across turns and bind it to booking.",
        turns=turns, before=before, after=after,
        verification={"created": created, "service": service.get("name")},
        deterministic_ok=ok, expected="One Candela booking is created.",
        issue_title="Laser device selection was not preserved into booking",
    )


def case_24_change_mind(db, ws):
    patient = new_patient(db, ws, first="ملك")
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w24_change_mind", [
        "ممكن احجز هيدرافيشل السبت؟",
        "لا استني، خليها PRP للبشرة بدل الهيدرافيشل",
    ])
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    ok = not created
    return make_result(
        scenario_id="w24_change_mind", category="booking_continuity",
        purpose="Changed service before slot confirmation replaces the old intent.",
        turns=turns, before=before, after=after,
        verification={"created": created}, deterministic_ok=ok,
        expected="No booking yet; corrected service owns the active flow.",
        issue_title="Old booking intent leaked into corrected request",
    )


def case_25_reschedule_time(db, ws):
    patient = new_patient(db, ws, first="دينا")
    service, _source_av, _source_slot, source = _seed_one(db, ws, patient)
    target_av, target = _availability_for(
        db, ws, service=service, doctor_id=source.doctor_id,
        after_date=source.start_at.date(),
    )
    day, time_text = local_slot(target_av, target)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w25_reschedule_time", [
        f"ممكن تغيري ميعادي؟ خليه يوم {day} الساعة {time_text}"
    ])
    after = extended_state_snapshot(db, ws, patient)
    replacements = _replacement_rows(after, source.id)
    ok = (
        _appointment_by_id(after, source.id)["status"] == "rescheduled"
        and len(replacements) == 1
    )
    return make_result(
        scenario_id="w25_reschedule_time", category="appointment_changes",
        purpose="Reschedule a single appointment to a verified free slot.",
        turns=turns, before=before, after=after,
        verification={"source_id": str(source.id), "replacements": replacements},
        deterministic_ok=ok,
        expected="Original becomes rescheduled and one replacement is created.",
        issue_title="Reschedule time lifecycle was incorrect",
    )


def case_26_reschedule_conflict(db, ws):
    patient = new_patient(db, ws, first="هبة")
    service, _source_av, _source_slot, source = _seed_one(db, ws, patient)
    target_av, target = _availability_for(
        db, ws, service=service, doctor_id=source.doctor_id,
        after_date=source.start_at.date(),
    )
    competitor = new_patient(db, ws, first="منافس")
    _seed_future_appointment(
        db, ws, competitor, service=service,
        doctor_id=source.doctor_id, slot=target,
    )
    day, time_text = local_slot(target_av, target)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w26_reschedule_conflict", [
        f"انقلي ميعادي ليوم {day} الساعة {time_text}"
    ])
    after = extended_state_snapshot(db, ws, patient)
    replacements = _replacement_rows(after, source.id)
    ok = (
        _appointment_by_id(after, source.id)["status"] == "confirmed"
        and not replacements
    )
    return make_result(
        scenario_id="w26_reschedule_conflict", category="appointment_changes",
        purpose="Reject a reschedule into another appointment's occupied slot.",
        turns=turns, before=before, after=after,
        verification={
            "source_id": str(source.id),
            "replacements": replacements,
            "conflict_start": target.start_at.isoformat(),
        },
        deterministic_ok=ok,
        expected="No replacement; original appointment remains confirmed.",
        issue_title="Conflicting reschedule was accepted",
    )


def case_27_ambiguous_reschedule(db, ws):
    patient = new_patient(db, ws, first="بسمة")
    s1, _av1, sl1, _ = _seed_one(db, ws, patient, "hydrafacial")
    s2 = service_by_slug(db, ws, "prp-skin")
    _av2, sl2 = _availability_for(
        db, ws, service=s2, after_date=sl1.start_at.date(),
    )
    _seed_future_appointment(
        db, ws, patient, service=s2,
        doctor_id=UUID(str(sl2.doctor_id)), slot=sl2,
    )
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w27_ambiguous_reschedule", [
        "عايزة أغير ميعادي الجاي"
    ])
    after = extended_state_snapshot(db, ws, patient)
    ok = before["appointments"] == after["appointments"]
    return make_result(
        scenario_id="w27_ambiguous_reschedule", category="appointment_changes",
        purpose="Multiple upcoming appointments require target clarification.",
        turns=turns, before=before, after=after,
        verification={"seed_services": [s1.name, s2.name]},
        deterministic_ok=ok,
        expected="No appointment changes before clarification.",
        issue_title="Ambiguous appointment change mutated state",
    )


def case_28_cancel_single(db, ws):
    patient = new_patient(db, ws, first="جنى")
    _service, _av, _slot, source = _seed_one(db, ws, patient)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w28_cancel_single", [
        "لو سمحتي الغي الحجز اللي جاي بتاعي"
    ])
    after = extended_state_snapshot(db, ws, patient)
    ok = _appointment_by_id(after, source.id)["status"] == "cancelled"
    return make_result(
        scenario_id="w28_cancel_single", category="cancellation",
        purpose="Cancel the only upcoming appointment.",
        turns=turns, before=before, after=after,
        verification={"source_id": str(source.id)}, deterministic_ok=ok,
        expected="Appointment becomes cancelled after a successful write.",
        issue_title="Single cancellation did not persist correctly",
    )


def case_29_cancel_ambiguous(db, ws):
    patient = new_patient(db, ws, first="رنا")
    _seed_one(db, ws, patient, "hydrafacial")
    service = service_by_slug(db, ws, "prp-skin")
    _av, slot = _availability_for(
        db, ws, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=5),
    )
    _seed_future_appointment(
        db, ws, patient, service=service,
        doctor_id=UUID(str(slot.doctor_id)), slot=slot,
    )
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w29_cancel_ambiguous", ["الغيلي الميعاد"])
    after = extended_state_snapshot(db, ws, patient)
    ok = before["appointments"] == after["appointments"]
    return make_result(
        scenario_id="w29_cancel_ambiguous", category="cancellation",
        purpose="Do not guess which appointment to cancel.",
        turns=turns, before=before, after=after,
        verification={}, deterministic_ok=ok,
        expected="No cancellation until target is clarified.",
        issue_title="Ambiguous cancellation changed appointment state",
    )


def case_30_cancel_already_cancelled(db, ws):
    patient = new_patient(db, ws, first="شهد")
    _service, _av, _slot, source = _seed_one(db, ws, patient)
    source.status = "cancelled"
    source.cancelled_at = datetime.now(UTC)
    db.flush()
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w30_cancel_already_cancelled", [
        "عايزة ألغي الميعاد ده كمان"
    ])
    after = extended_state_snapshot(db, ws, patient)
    ok = (
        _appointment_by_id(after, source.id)["status"] == "cancelled"
        and before["appointments"] == after["appointments"]
    )
    return make_result(
        scenario_id="w30_cancel_already_cancelled", category="cancellation",
        purpose="Do not claim a new cancellation for already-cancelled state.",
        turns=turns, before=before, after=after,
        verification={"source_id": str(source.id)}, deterministic_ok=ok,
        expected="No new lifecycle mutation or false acknowledgment.",
        issue_title="Already-cancelled appointment was mishandled",
    )


def case_31_package_booking(db, ws):
    patient = named_patient(db, ws, "Omar", "Ahmed")
    service = service_by_slug(db, ws, "prp-skin")
    available, slot = _availability_for(db, ws, service=service)
    doctor = _doctor_row(build_clinic_catalog(db, ws), slot.doctor_id)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w31_package_booking", [
        f"احجزيلي PRP للبشرة يوم {day} الساعة {time_text} مع {doctor_name(doctor)} وخليها من الباكدج"
    ])
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    usage_created = (delta.get("package_usages") or {}).get("created") or []
    ok = (
        len(created) == 1
        and bool(usage_created)
        and created[0]["billing_context"] == "package_prepaid"
    )
    return make_result(
        scenario_id="w31_package_booking", category="packages",
        purpose="Book against an actual usable PRP package.",
        turns=turns, before=before, after=after,
        verification={"created": created, "package_usage_created": usage_created},
        deterministic_ok=ok,
        expected="Reserve one package session without treating it as a new payment.",
        issue_title="Package-aware booking accounting was incorrect",
    )


def case_32_pulse_no_auto_use(db, ws):
    patient = named_patient(db, ws, "gggg", "")
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w32_pulse_no_auto_use", [
        "عندي pulse على Prime بس لو حجزت ليزر عايزة أدفع الجلسة عادي، ما تخصميش من الرصيد"
    ])
    after = extended_state_snapshot(db, ws, patient)
    pulse_delta = db_delta(before, after).get("pulse_balance_delta") or {}
    ok = not pulse_delta
    return make_result(
        scenario_id="w32_pulse_no_auto_use", category="pulse",
        purpose="Explicitly declining Pulse consumption must preserve balance.",
        turns=turns, before=before, after=after,
        verification={"pulse_balance_delta": pulse_delta},
        deterministic_ok=ok,
        expected="Pulse balance remains unchanged without explicit consumption.",
        issue_title="Pulse balance was consumed automatically",
    )


def case_33_stale_availability(db, ws):
    patient = new_patient(db, ws, first="لمياء")
    service = service_by_slug(db, ws, "hydrafacial")
    available, slot = _availability_for(db, ws, service=service)
    doctor = _doctor_row(build_clinic_catalog(db, ws), slot.doctor_id)
    day, time_text = local_slot(available, slot)
    response, first = send_turn(
        db, ws, patient, "w33_stale_availability", 1,
        f"الهيدرافيشل يوم {day} الساعة {time_text} مع {doctor_name(doctor)} فاضي؟",
        None,
    )
    competitor = new_patient(db, ws, first="حجز")
    _seed_future_appointment(
        db, ws, competitor, service=service,
        doctor_id=UUID(str(slot.doctor_id)), slot=slot,
    )
    before = extended_state_snapshot(db, ws, patient)
    _r2, second = send_turn(
        db, ws, patient, "w33_stale_availability", 2,
        "تمام احجزيه", response.conversation_id,
    )
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    ok = not created
    return make_result(
        scenario_id="w33_stale_availability", category="stale_context",
        purpose="Fresh backend availability overrides earlier conversation truth.",
        turns=[first, second], before=before, after=after,
        verification={"created": created, "stale_start": slot.start_at.isoformat()},
        deterministic_ok=ok,
        expected="No double booking after slot becomes occupied.",
        issue_title="Stale availability was used as write permission",
    )


def case_34_stale_time_claim(db, ws):
    patient = new_patient(db, ws, first="نادين")
    _service, _av, _slot, source = _seed_one(db, ws, patient)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w34_stale_time_claim", [
        "ميعادي الجاي امتى؟",
        "لا أنا فاكرة إنه الساعة ٤، اتأكدي كده",
    ])
    after = extended_state_snapshot(db, ws, patient)
    ok = before["appointments"] == after["appointments"]
    return make_result(
        scenario_id="w34_stale_time_claim", category="stale_context",
        purpose="Current appointment truth beats customer's incorrect remembered time.",
        turns=turns, before=before, after=after,
        verification={"actual_start_at": source.start_at.isoformat()},
        deterministic_ok=ok,
        expected="Verified current time is preserved.",
        issue_title="Stale customer context overrode appointment truth",
    )


def case_35_blocked_patient(db, ws):
    patient = new_patient(db, ws, first="موقوف", status="blocked")
    service = service_by_slug(db, ws, "hydrafacial")
    available, slot = _availability_for(db, ws, service=service)
    doctor = _doctor_row(build_clinic_catalog(db, ws), slot.doctor_id)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w35_blocked_patient", [
        f"احجزيلي هيدرافيشل يوم {day} الساعة {time_text} مع {doctor_name(doctor)}"
    ])
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    ok = not created
    return make_result(
        scenario_id="w35_blocked_patient", category="write_safety",
        purpose="Blocked patient must not receive a booking write.",
        turns=turns, before=before, after=after,
        verification={"created": created}, deterministic_ok=ok,
        expected="No appointment is created.",
        issue_severity="P0", issue_title="Blocked patient received booking write",
    )


def case_36_continuity_correction(db, ws):
    patient = new_patient(db, ws, first="تاليا")
    before = extended_state_snapshot(db, ws, patient)
    turns = _messages(db, ws, patient, "w36_continuity_correction", [
        "عايزة احجز ليزر للوجه",
        "Prime Lase",
        "لا معلش كانديلا، خليها Candela",
        "والسبت بعد ٦ ينفع؟",
    ])
    after = extended_state_snapshot(db, ws, patient)
    created = created_appointments(before, after)
    ok = len(created) <= 1
    return make_result(
        scenario_id="w36_continuity_correction", category="continuity",
        purpose="Latest corrected device choice wins across short follow-ups.",
        turns=turns, before=before, after=after,
        verification={"created": created}, deterministic_ok=ok,
        expected="No stale device reversion or duplicate booking.",
        issue_title="Multi-turn correction produced stale/duplicate state",
    )


CASES = [
    case_21_simple_booking, case_22_ambiguous_booking,
    case_23_laser_device_booking, case_24_change_mind,
    case_25_reschedule_time, case_26_reschedule_conflict,
    case_27_ambiguous_reschedule, case_28_cancel_single,
    case_29_cancel_ambiguous, case_30_cancel_already_cancelled,
    case_31_package_booking, case_32_pulse_no_auto_use,
    case_33_stale_availability, case_34_stale_time_claim,
    case_35_blocked_patient, case_36_continuity_correction,
]


if __name__ == "__main__":
    raise SystemExit(run_group(CASES, output_name="readiness_writes_20260930"))
