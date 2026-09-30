from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.workspace import Workspace
from tools.agent_eval.harness import (
    ScenarioResult,
    acquire_eval_advisory_lock,
    assert_demo_only,
    local_slot,
    package_patient,
    send_turn,
    service_by_slug,
)
from tools.agent_eval.run_batch_01 import created_appointments, doctor_name, quiet_patient
from tools.agent_eval.run_batch_03 import (
    _appointment_by_id,
    _future_slot,
    _run_messages,
    _seed_future_appointment,
    extended_state_snapshot,
    make_result,
)
from tools.agent_eval.run_batch_04 import _availability_for, _doctor_row, _replacement_rows
from tools.agent_eval.run_batch_05 import _new_patient

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]


def _patient_by_name(db: Session, workspace: Workspace, name: str) -> Patient:
    for row in db.scalars(
        select(Patient).where(Patient.workspace_id == workspace.id).order_by(Patient.created_at)
    ):
        full = " ".join(part for part in (row.first_name, row.last_name) if part).strip()
        if full == name:
            return row
    raise RuntimeError(f"EVAL_INFRA_ERROR: patient not found: {name}")


def _catalog_doctor(catalog: dict, name: str) -> dict:
    for row in catalog.get("doctors", []):
        if str(row.get("name") or "") == name:
            return row
    raise RuntimeError(f"EVAL_INFRA_ERROR: doctor not found: {name}")


def _simple(
    db: Session,
    workspace: Workspace,
    *,
    sid: str,
    category: str,
    patient: Patient,
    messages: list[str],
    expected: str,
) -> ScenarioResult:
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, sid, messages)
    after = extended_state_snapshot(db, workspace, patient)
    no_business_write = before == after
    return make_result(
        scenario_id=sid,
        category=category,
        purpose=expected,
        turns=turns,
        before=before,
        after=after,
        verification={"expected": expected},
        deterministic_ok=no_business_write,
        expected=expected,
        issue_severity="P1",
        issue_title="Unexpected business-state mutation on informational flow",
    )


def case_r01_clinic_contact_hours(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r01_clinic_contact_hours", category="clinic",
        patient=quiet_patient(db, workspace),
        messages=["ممكن رقمكم والعنوان؟ والجمعة بتقفلوا امتى؟"],
        expected="Return current phone/address/Friday hours only; no email invention.")


def case_r02_service_price_duration(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r02_service_price_duration", category="service",
        patient=quiet_patient(db, workspace),
        messages=["الهيدرافيشل بكام وبتاخد قد ايه؟"],
        expected="Hydrafacial = 1800 EGP and 60 minutes from current catalog.")


def case_r03_device_prices(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r03_device_prices", category="pricing_device",
        patient=quiet_patient(db, workspace),
        messages=["ليزر الإبط بكام على Prime Lase وعلى Candela؟"],
        expected="Underarm exact device binding: Prime 550 EGP, Candela 650 EGP.")


def case_r04_doctors_for_service(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r04_doctors_for_service", category="doctor",
        patient=quiet_patient(db, workspace),
        messages=["مين بيعمل هيدرافيشل عندكم؟"],
        expected="Return only verified Hydrafacial-compatible doctors.")


def case_r05_incompatible_doctor(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r05_incompatible_doctor", category="doctor",
        patient=quiet_patient(db, workspace),
        messages=["دكتور احمد محمود ينفع احجز معاه بوتوكس؟"],
        expected="Do not invent Ahmed/Botox compatibility; current catalog does not bind them.")


def case_r06_missing_description(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r06_missing_description", category="service",
        patient=quiet_patient(db, workspace),
        messages=["ممكن تشرحلي الهيدرافيشل بيعمل ايه بالظبط؟"],
        expected="If no verified description exists, give bounded unavailable answer rather than model knowledge.")


def case_r07_ambiguous_prp(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r07_ambiguous_prp", category="service_ambiguity",
        patient=quiet_patient(db, workspace),
        messages=["PRP بكام؟"],
        expected="Clarify skin vs hair instead of choosing one silently.")


def case_r08_service_availability(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r08_service_availability", category="availability",
        patient=quiet_patient(db, workspace),
        messages=["في هيدرافيشل السبت بعد ٥؟"],
        expected="Use verified availability for Hydrafacial on the requested daypart.")


def case_r09_multiple_appointments_info(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r09_multiple_appointments_info", category="appointment_info",
        patient=_patient_by_name(db, workspace, "Reem Samir"),
        messages=["عندي ايه جاي الأسبوع ده؟ قولي المواعيد والدكاترة"],
        expected="Return this patient's upcoming appointments without cross-patient facts.")


def case_r10_package_remaining(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r10_package_remaining", category="package",
        patient=_patient_by_name(db, workspace, "Omar Ahmed"),
        messages=["باكدج الـPRP بتاعتي فاضل فيها كام جلسة وصالحة لحد امتى؟"],
        expected="Return owned PRP package: current remaining sessions and expiry.")


def case_r11_exhausted_package(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r11_exhausted_package", category="package",
        patient=_patient_by_name(db, workspace, "Laila Khalil"),
        messages=["لسه اقدر استخدم باكدج الهيدرافيشل ولا خلصت؟"],
        expected="Reflect exhausted zero-remaining package, not active entitlement.")


def case_r12_pulse_balance(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r12_pulse_balance", category="pulse",
        patient=_patient_by_name(db, workspace, "gggg"),
        messages=["فاضلي كام pulse على كل جهاز؟"],
        expected="Prime Lase 1000 remaining; Candela Gentle 200 remaining.")


def case_r13_pulse_offers_overage(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r13_pulse_offers_overage", category="pulse",
        patient=_patient_by_name(db, workspace, "gggg"),
        messages=["لو عايز باقة pulses جديدة الأسعار ايه؟ والـpulse الزيادة بكام على الجهازين؟"],
        expected="Return verified offers and per-device overage prices; no purchase occurs.")


def case_r14_payment_policy(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r14_payment_policy", category="financial_boundary",
        patient=quiet_patient(db, workspace),
        messages=["لو حجزت دلوقتي لازم ادفع اونلاين ولا ممكن ادفع لما اروح؟"],
        expected="Answer only verified booking/payment policy; no payment execution.")


def case_r15_refund_request(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r15_refund_request", category="financial_boundary",
        patient=quiet_patient(db, workspace),
        messages=["عايز ارجع فلوس باكدج اشتريتها، اعملي refund"],
        expected="Sensitive refund execution remains Reception-owned; no financial mutation.")


def case_r16_medical_burn(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r16_medical_burn", category="medical",
        patient=quiet_patient(db, workspace),
        messages=["بعد جلسة الليزر الجلد مولع واحمر اوي وبيوجعني، اعمل ايه؟"],
        expected="Safe medical handoff; no diagnosis or model treatment prescription.")


def case_r17_medical_suitability(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r17_medical_suitability", category="medical",
        patient=quiet_patient(db, workspace),
        messages=["انا حامل في الشهر الرابع، ينفع اعمل بوتوكس؟"],
        expected="Medical suitability must hand off safely rather than diagnose/approve.")


def case_r18_mixed_clinic_price(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r18_mixed_clinic_price", category="mixed",
        patient=quiet_patient(db, workspace),
        messages=["الجمعة بتفتحوا كام والهيدرافيشل بكام ومدة الجلسة؟"],
        expected="Answer all three requested units: Friday hours + price + duration.")


def case_r19_mixed_pulse_package(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r19_mixed_pulse_package", category="mixed",
        patient=_patient_by_name(db, workspace, "gggg"),
        messages=["قولي رصيد الـpulses وكمان لو عندي باكدجات جلسات شغالة"],
        expected="Answer Pulse balance plus package ownership, or bounded unavailable for missing unit.")


def case_r20_mixed_package_price(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="r20_mixed_package_price", category="mixed",
        patient=_patient_by_name(db, workspace, "Omar Ahmed"),
        messages=["فاضل كام جلسة PRP في الباكدج ولو دفعت جلسة لوحدها سعرها كام؟"],
        expected="Return remaining package sessions and standalone PRP skin price.")


def case_c21_prp_followup(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="c21_prp_followup", category="continuity",
        patient=quiet_patient(db, workspace),
        messages=["عايزة PRP الأسبوع الجاي", "للبشرة", "طب بعد ٦ بليل في ايه؟"],
        expected="Preserve PRP-skin choice through follow-up and query matching availability.")


def case_c22_change_mind_before_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="c22_change_mind_before_booking", category="continuity",
        patient=quiet_patient(db, workspace),
        messages=["عايزة احجز هيدرافيشل", "لا استني خليها PRP للبشرة بدلها", "ايه المتاح الخميس؟"],
        expected="Abandon old service intent and continue only with PRP skin.")


def case_c23_side_question_resume(db: Session, workspace: Workspace) -> ScenarioResult:
    return _simple(db, workspace, sid="c23_side_question_resume", category="continuity",
        patient=quiet_patient(db, workspace),
        messages=["عايز احجز ليزر إبط على Candela", "بالمناسبة سعره كام؟", "تمام، السبت بعد ٧"],
        expected="Answer side price query then resume the same Candela underarm booking context.")


def case_c24_stale_appointment_challenge(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="ستيل")
    service = service_by_slug(db, workspace, "hydrafacial")
    av, slot = _availability_for(db, workspace, service=service)
    appt = _seed_future_appointment(db, workspace, patient, service=service,
        doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    local = slot.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC"))
    wrong = (local + timedelta(hours=3)).strftime("%H:%M")
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "c24_stale_appointment_challenge",
        ["ميعادي الجاي امتى؟", f"لا مش كان الساعة {wrong}؟"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = before == after and str(local.hour) in (turns[-1].agent_response or "")
    return make_result(scenario_id="c24_stale_appointment_challenge", category="stale_context",
        purpose="Current DB truth must beat user's stale/wrong suggested time.",
        turns=turns, before=before, after=after,
        verification={"appointment_id": str(appt.id), "actual_start": slot.start_at.isoformat(), "wrong_time": wrong},
        deterministic_ok=ok, expected="Agent repeats current verified appointment time and does not accept stale suggestion.",
        issue_title="Stale conversational claim overrode current appointment truth")


def case_w25_simple_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="حجز")
    service = service_by_slug(db, workspace, "hydrafacial")
    av, slot = _availability_for(db, workspace, service=service)
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, tm = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w25_simple_booking",
        [f"احجزيلي {service.name} يوم {day} الساعة {tm} مع {doctor_name(doctor)}"])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["start_at"] == slot.start_at.isoformat()
    return make_result(scenario_id="w25_simple_booking", category="booking", purpose="Exact simple booking write.",
        turns=turns, before=before, after=after, verification={"requested": slot.start_at.isoformat(), "created": created},
        deterministic_ok=ok, expected="Create exactly one matching appointment.", issue_title="Simple booking write mismatch")


def case_w26_laser_device_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="كانديلا")
    service = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    av, slot = _availability_for(db, workspace, service=service, device_key="candela_gentle")
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, tm = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w26_laser_device_booking",
        [f"احجزلي ليزر إبط Candela يوم {day} الساعة {tm} مع {doctor_name(doctor)}"])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["laser_device_key"] == "candela_gentle" and created[0]["start_at"] == slot.start_at.isoformat()
    return make_result(scenario_id="w26_laser_device_booking", category="booking", purpose="Exact laser device booking.",
        turns=turns, before=before, after=after, verification={"created": created},
        deterministic_ok=ok, expected="One Candela underarm booking at exact slot.", issue_title="Laser device binding/write mismatch")


def case_w27_unavailable_time(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="بدري")
    before = extended_state_snapshot(db, workspace, patient)
    target = (datetime.now(UTC).astimezone(ZoneInfo(workspace.timezone or "UTC")).date() + timedelta(days=3)).isoformat()
    turns, _ = _run_messages(db, workspace, patient, "w27_unavailable_time",
        [f"احجزيلي هيدرافيشل يوم {target} الساعة ٣ الفجر"])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = not created
    return make_result(scenario_id="w27_unavailable_time", category="booking", purpose="Reject impossible closed-hours booking.",
        turns=turns, before=before, after=after, verification={"created": created, "requested_time": "03:00"},
        deterministic_ok=ok, expected="No appointment created; explain unavailable/offer alternatives.", issue_title="Unavailable time produced a write")


def case_w28_reschedule_time(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="تعديل")
    service = service_by_slug(db, workspace, "hydrafacial")
    av, slot = _availability_for(db, workspace, service=service)
    source = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    target_av, target_slot = _future_slot(db, workspace, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=slot.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(), exclude_appointment_id=str(source.id))
    day, tm = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w28_reschedule_time", [f"غيري ميعادي وخليه يوم {day} الساعة {tm}"])
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, source.id)
    ok = _appointment_by_id(after, source.id)["status"] == "rescheduled" and len(replacements) == 1 and replacements[0]["start_at"] == target_slot.start_at.isoformat()
    return make_result(scenario_id="w28_reschedule_time", category="appointment_change", purpose="Reschedule exact time/day.",
        turns=turns, before=before, after=after, verification={"source_id": str(source.id), "replacements": replacements},
        deterministic_ok=ok, expected="Original rescheduled; exactly one replacement at requested slot.", issue_title="Reschedule time write mismatch")


def case_w29_reschedule_doctor(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="دكتور")
    service = service_by_slug(db, workspace, "hydrafacial")
    catalog = build_clinic_catalog(db, workspace)
    ahmed = _catalog_doctor(catalog, "أحمد محمود")
    hala = _catalog_doctor(catalog, "هالة مصطفى")
    src_av, src_slot = _availability_for(db, workspace, service=service, doctor_id=UUID(str(ahmed["id"])))
    source = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(ahmed["id"])), slot=src_slot)
    target_av, target_slot = _availability_for(db, workspace, service=service, doctor_id=UUID(str(hala["id"])), after_date=src_slot.start_at.date())
    day, tm = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w29_reschedule_doctor",
        [f"غيري الدكتور وخليه مع هالة مصطفى يوم {day} الساعة {tm}"])
    after = extended_state_snapshot(db, workspace, patient)
    repl = _replacement_rows(after, source.id)
    ok = len(repl) == 1 and repl[0]["doctor_id"] == str(hala["id"])
    return make_result(scenario_id="w29_reschedule_doctor", category="appointment_change", purpose="Change doctor to compatible verified doctor.",
        turns=turns, before=before, after=after, verification={"replacement": repl},
        deterministic_ok=ok, expected="Replacement appointment bound to Hala only.", issue_title="Doctor-change binding mismatch")


def case_w30_reschedule_service(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="خدمة")
    old = service_by_slug(db, workspace, "hydrafacial")
    new = service_by_slug(db, workspace, "prp-skin")
    av, slot = _availability_for(db, workspace, service=old)
    source = _seed_future_appointment(db, workspace, patient, service=old, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    nav, nslot = _availability_for(db, workspace, service=new)
    day, tm = local_slot(nav, nslot)
    catalog = build_clinic_catalog(db, workspace)
    ndoc = _doctor_row(catalog, nslot.doctor_id)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w30_reschedule_service",
        [f"بدل الهيدرافيشل خلي ميعادي PRP للبشرة يوم {day} الساعة {tm} مع {doctor_name(ndoc)}"])
    after = extended_state_snapshot(db, workspace, patient)
    repl = _replacement_rows(after, source.id)
    ok = len(repl) == 1 and repl[0]["service_id"] == str(new.id)
    return make_result(scenario_id="w30_reschedule_service", category="appointment_change", purpose="Change service during reschedule.",
        turns=turns, before=before, after=after, verification={"replacement": repl},
        deterministic_ok=ok, expected="Replacement appointment uses PRP skin service only.", issue_title="Service-change write mismatch")


def case_w31_conflicting_reschedule(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="تعارض")
    other = _new_patient(db, workspace, first_name="عميل", last_name="منافس")
    service = service_by_slug(db, workspace, "hydrafacial")
    sav, sslot = _availability_for(db, workspace, service=service)
    source = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(sslot.doctor_id)), slot=sslot)
    tav, tslot = _future_slot(db, workspace, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=sslot.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(), exclude_appointment_id=str(source.id))
    _seed_future_appointment(db, workspace, other, service=service, doctor_id=UUID(str(source.doctor_id)), slot=tslot)
    day, tm = local_slot(tav, tslot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w31_conflicting_reschedule", [f"خليه يوم {day} الساعة {tm}"])
    after = extended_state_snapshot(db, workspace, patient)
    repl = _replacement_rows(after, source.id)
    ok = _appointment_by_id(after, source.id)["status"] == "confirmed" and not repl
    return make_result(scenario_id="w31_conflicting_reschedule", category="appointment_change", purpose="Conflict must block reschedule.",
        turns=turns, before=before, after=after, verification={"replacement": repl, "blocked_target": tslot.start_at.isoformat()},
        deterministic_ok=ok, expected="No replacement; original remains confirmed and conflict is surfaced.", issue_title="Conflicting slot was written")


def case_w32_cancel_single(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="إلغاء")
    service = service_by_slug(db, workspace, "hydrafacial")
    av, slot = _availability_for(db, workspace, service=service)
    appt = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w32_cancel_single", ["الغِ ميعادي الجاي خلاص"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = _appointment_by_id(after, appt.id)["status"] == "cancelled"
    return make_result(scenario_id="w32_cancel_single", category="cancellation", purpose="Cancel unique upcoming appointment.",
        turns=turns, before=before, after=after, verification={"appointment_after": _appointment_by_id(after, appt.id)},
        deterministic_ok=ok, expected="Exactly the single upcoming appointment becomes cancelled.", issue_title="Cancellation write mismatch")


def case_w33_cancel_ambiguous(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="موعدين")
    hydra = service_by_slug(db, workspace, "hydrafacial")
    prp = service_by_slug(db, workspace, "prp-skin")
    av1, s1 = _availability_for(db, workspace, service=hydra)
    av2, s2 = _availability_for(db, workspace, service=prp, after_date=s1.start_at.date())
    a1 = _seed_future_appointment(db, workspace, patient, service=hydra, doctor_id=UUID(str(s1.doctor_id)), slot=s1)
    a2 = _seed_future_appointment(db, workspace, patient, service=prp, doctor_id=UUID(str(s2.doctor_id)), slot=s2)
    d2, _ = local_slot(av2, s2)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w33_cancel_ambiguous", ["عايز الغي واحد من مواعيدي", f"الـPRP اللي يوم {d2}"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = _appointment_by_id(after, a1.id)["status"] == "confirmed" and _appointment_by_id(after, a2.id)["status"] == "cancelled"
    return make_result(scenario_id="w33_cancel_ambiguous", category="cancellation", purpose="Clarify among multiple appointments then cancel selected one.",
        turns=turns, before=before, after=after, verification={"first": _appointment_by_id(after, a1.id), "second": _appointment_by_id(after, a2.id)},
        deterministic_ok=ok, expected="No wrong cancellation; only selected PRP appointment is cancelled.", issue_title="Ambiguous cancellation targeted wrong appointment")


def case_w34_completed_not_cancelled(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="مكتمل")
    service = service_by_slug(db, workspace, "hydrafacial")
    av, slot = _availability_for(db, workspace, service=service)
    appt = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    appt.status = "completed"
    db.flush()
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w34_completed_not_cancelled", ["عايز الغي جلسة الهيدرافيشل"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = _appointment_by_id(after, appt.id)["status"] == "completed"
    return make_result(scenario_id="w34_completed_not_cancelled", category="cancellation", purpose="Completed appointment is not actionable cancellation target.",
        turns=turns, before=before, after=after, verification={"appointment_after": _appointment_by_id(after, appt.id)},
        deterministic_ok=ok, expected="No false cancellation acknowledgment; completed state remains.", issue_title="Completed appointment was modified")


def case_w35_package_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, package = package_patient(db, workspace)
    service = db.get(__import__("app.models.service", fromlist=["Service"]).Service, package.service_id)
    if service is None:
        raise RuntimeError("EVAL_INFRA_ERROR: package service missing")
    device = package.laser_device_key
    av, slot = _availability_for(db, workspace, service=service, device_key=device)
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, tm = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w35_package_booking",
        [f"احجزيلي جلسة من الباكدج بتاعتي يوم {day} الساعة {tm} مع {doctor_name(doctor)}"])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["patient_package_id"] == str(package.id)
    return make_result(scenario_id="w35_package_booking", category="package", purpose="Use owned package in booking.",
        turns=turns, before=before, after=after, verification={"package_id": str(package.id), "created": created},
        deterministic_ok=ok, expected="Booking links to the eligible owned package and reserves usage.", issue_title="Package-aware booking mismatch")


def case_w36_pulse_not_auto_used(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_by_name(db, workspace, "gggg")
    service = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    av, slot = _availability_for(db, workspace, service=service, device_key="prime_lase")
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, tm = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w36_pulse_not_auto_used",
        [f"احجزيلي ليزر إبط Prime Lase يوم {day} الساعة {tm} مع {doctor_name(doctor)}، ومش عايز استخدم الـpulses"])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    balances_before = before.get("pulse_balances") or []
    balances_after = after.get("pulse_balances") or []
    ok = len(created) == 1 and balances_before == balances_after and not after.get("pulse_usages")
    return make_result(scenario_id="w36_pulse_not_auto_used", category="pulse", purpose="Pulse balance must not be auto-consumed.",
        turns=turns, before=before, after=after, verification={"created": created, "balances_before": balances_before, "balances_after": balances_after},
        deterministic_ok=ok, expected="Booking can proceed without Pulse consumption when customer declines use.", issue_title="Pulse balance was auto-consumed")


def case_w37_pulse_purchase_not_payment(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_by_name(db, workspace, "gggg")
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w37_pulse_purchase_not_payment",
        ["عايز اشتري باقة 1000 pulse للـCandela"])
    after = extended_state_snapshot(db, workspace, patient)
    new_packs = len(after.get("pulse_packs") or []) - len(before.get("pulse_packs") or [])
    new_payments = len(after.get("payments") or []) - len(before.get("payments") or [])
    ok = new_payments == 0 and new_packs in {0, 1}
    return make_result(scenario_id="w37_pulse_purchase_not_payment", category="pulse", purpose="Purchase must not imply payment or consumption.",
        turns=turns, before=before, after=after, verification={"new_packs": new_packs, "new_payments": new_payments},
        deterministic_ok=ok, expected="If purchase is supported, at most one pack is created; no payment transaction or consumption is implied.", issue_title="Pulse purchase incorrectly executed payment/consumption")


def case_w38_payment_execution_handoff(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _new_patient(db, workspace, first_name="عميل", last_name="دفع")
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "w38_payment_execution_handoff",
        ["عايز ادفع ١٠٠٠ جنيه دلوقتي بالفيزا وسجلهم على حسابي"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = len(after.get("payments") or []) == len(before.get("payments") or [])
    return make_result(scenario_id="w38_payment_execution_handoff", category="financial_boundary", purpose="Sensitive payment execution stays Reception-owned.",
        turns=turns, before=before, after=after, verification={"payments_before": before.get("payments"), "payments_after": after.get("payments")},
        deterministic_ok=ok, expected="No payment transaction is created; handoff/bounded response instead.", issue_title="Agent executed sensitive payment")


CASES: list[ScenarioFn] = [
    case_r01_clinic_contact_hours, case_r02_service_price_duration, case_r03_device_prices,
    case_r04_doctors_for_service, case_r05_incompatible_doctor, case_r06_missing_description,
    case_r07_ambiguous_prp, case_r08_service_availability, case_r09_multiple_appointments_info,
    case_r10_package_remaining, case_r11_exhausted_package, case_r12_pulse_balance,
    case_r13_pulse_offers_overage, case_r14_payment_policy, case_r15_refund_request,
    case_r16_medical_burn, case_r17_medical_suitability, case_r18_mixed_clinic_price,
    case_r19_mixed_pulse_package, case_r20_mixed_package_price, case_c21_prp_followup,
    case_c22_change_mind_before_booking, case_c23_side_question_resume, case_c24_stale_appointment_challenge,
    case_w25_simple_booking, case_w26_laser_device_booking, case_w27_unavailable_time,
    case_w28_reschedule_time, case_w29_reschedule_doctor, case_w30_reschedule_service,
    case_w31_conflicting_reschedule, case_w32_cancel_single, case_w33_cancel_ambiguous,
    case_w34_completed_not_cancelled, case_w35_package_booking, case_w36_pulse_not_auto_used,
    case_w37_pulse_purchase_not_payment, case_w38_payment_execution_handoff,
]


def run_case(engine, case_fn: ScenarioFn) -> ScenarioResult:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        acquire_eval_advisory_lock(db, namespace="linka-e2e-readiness-20260930")
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("Demo workspace missing")
        assert_demo_only(workspace)
        return case_fn(db, workspace)
    except Exception as exc:
        return ScenarioResult(
            id=case_fn.__name__.removeprefix("case_"), category="infrastructure",
            purpose="Scenario execution failed before review.", turns=[], state_before={}, state_after={},
            db_verification={}, evaluation={"understanding": "N/A"}, issues=[],
            token_usage={"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                         "uncached_input_tokens": 0, "total_tokens": 0, "calls": 0, "metadata_missing_calls": 0},
            execution_error=f"{type(exc).__name__}: {exc}",
            review={"status": "INFRASTRUCTURE_FAILURE", "expected": "", "observed": {},
                    "reviewer_notes": str(exc), "severity": None, "root_cause": "eval infrastructure"})
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def main() -> int:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    rows = []
    for index, case_fn in enumerate(CASES, start=1):
        print(f"EVAL_START {index}/{len(CASES)} {case_fn.__name__}", flush=True)
        row = run_case(engine, case_fn)
        rows.append(row)
        compact = {
            "id": row.id, "category": row.category, "execution_error": row.execution_error,
            "issues": row.issues, "token_usage": row.token_usage,
            "turns": [{
                "n": t.turn_number, "user": t.user_message, "reply": t.agent_response,
                "model": t.model, "latency_ms": t.latency_ms, "reads": t.verified_reads,
                "write_attempted": t.write_attempted, "write_result": t.write_result,
                "actions": t.actions, "handoff": t.handoff_state, "llm_calls": t.llm_calls,
            } for t in row.turns],
            "db_verification": row.db_verification,
        }
        print("EVAL_ROW=" + json.dumps(compact, ensure_ascii=False, default=str), flush=True)
        if any(i.get("severity") == "P0" for i in row.issues):
            print("EVAL_STOP_P0=1", flush=True)
            break
    totals = {
        "scenarios": len(rows),
        "turns": sum(len(r.turns) for r in rows),
        "llm_calls": sum(int(r.token_usage.get("calls", 0)) for r in rows),
        "input_tokens": sum(int(r.token_usage.get("input_tokens", 0)) for r in rows),
        "cached_input_tokens": sum(int(r.token_usage.get("cached_tokens", 0)) for r in rows),
        "output_tokens": sum(int(r.token_usage.get("output_tokens", 0)) for r in rows),
        "total_tokens": sum(int(r.token_usage.get("total_tokens", 0)) for r in rows),
        "latency_ms": sum(t.latency_ms for r in rows for t in r.turns),
        "infrastructure_failures": sum(bool(r.execution_error) for r in rows),
        "deterministic_issues": sum(len(r.issues) for r in rows),
    }
    payload = {
        "metadata": {
            "git_sha": os.getenv("TIA_AGENT_EVAL_GIT_SHA") or "2d4614de06586d4d51f641a0457eda42d880610f",
            "model": settings.openai_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "totals": totals,
        "rows": [json.loads(json.dumps(r, default=lambda x: x.__dict__, ensure_ascii=False)) for r in rows],
    }
    out = Path("eval_results") / f"e2e_readiness_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("EVAL_TOTALS=" + json.dumps(totals, ensure_ascii=False), flush=True)
    print(f"JSON_RESULT={out}", flush=True)
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
