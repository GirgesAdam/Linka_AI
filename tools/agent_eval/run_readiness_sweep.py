from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.message import Message
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import PatientPulsePack, PulsePackOffer, PulseUsage
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.models.workspace import Workspace
from app.services.pulse_billing import list_patient_pulse_balances

from tools.agent_eval.harness import ScenarioResult, assert_demo_only, jsonable, local_slot, send_turn
from tools.agent_eval.run_batch_01 import created_appointments, doctor_name, quiet_patient
from tools.agent_eval.run_batch_02 import laser_context
from tools.agent_eval.run_batch_03 import (
    _future_slot,
    _run_messages,
    _seed_future_appointment,
    _seed_history_conversation,
    db_delta,
    extended_state_snapshot,
    make_result,
    run_case,
)
from tools.agent_eval.run_batch_04 import _availability_for, _doctor_row, _replacement_rows

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
WORKSPACE_SLUG = "tia"


def _patient_named(db: Session, workspace: Workspace, full_name: str) -> Patient:
    parts = full_name.split(maxsplit=1)
    first, last = parts[0], parts[1] if len(parts) > 1 else ""
    row = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace.id,
            Patient.first_name == first,
            Patient.last_name == last,
        ).order_by(Patient.created_at.asc()).limit(1)
    )
    if row is None:
        raise RuntimeError(f"EVAL_INFRA_ERROR: patient not found: {full_name}")
    return row


def _reply(turns) -> str:
    return "\n".join((turn.agent_response or "") for turn in turns)


def _business_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    delta = db_delta(before, after)
    for key in ("appointments", "packages", "package_usages", "pulse_packs", "payments", "pulse_usages", "pulse_settlements"):
        item = delta.get(key) or {}
        if item.get("created") or item.get("removed") or item.get("changed"):
            return False
    return not bool(delta.get("pulse_balance_delta"))


def _review(row: ScenarioResult, *, initial: Any, intent: str, reads: list[str],
            writes: str, final_state: str, response_facts: list[str]) -> ScenarioResult:
    row.review.update({
        "initial_state": initial,
        "expected_intent": intent,
        "expected_reads": reads,
        "expected_writes": writes,
        "expected_final_db_state": final_state,
        "expected_response_facts": response_facts,
        "naturalness": "PENDING_MANUAL_REVIEW",
    })
    return row


def _info_case(
    scenario_id: str,
    category: str,
    messages: list[str],
    *,
    patient_name: str | None = None,
    must_have: tuple[str, ...] = (),
    must_not_have: tuple[str, ...] = (),
    expected_reads: tuple[str, ...] = (),
    expect_handoff: bool = False,
) -> ScenarioFn:
    def case(db: Session, workspace: Workspace) -> ScenarioResult:
        patient = _patient_named(db, workspace, patient_name) if patient_name else quiet_patient(db, workspace)
        before = extended_state_snapshot(db, workspace, patient)
        turns, _ = _run_messages(db, workspace, patient, scenario_id, messages)
        after = extended_state_snapshot(db, workspace, patient)
        text = _reply(turns).casefold()
        factual = all(value.casefold() in text for value in must_have)
        clean = all(value.casefold() not in text for value in must_not_have)
        handoff_ok = (any(turn.handoff_state for turn in turns) if expect_handoff else True)
        ok = _business_unchanged(before, after) and factual and clean and handoff_ok
        row = make_result(
            scenario_id=scenario_id,
            category=category,
            purpose="Fresh realistic read-only / boundary evaluation.",
            turns=turns,
            before=before,
            after=after,
            verification={
                "reply_contains_required_facts": factual,
                "reply_avoids_forbidden_facts": clean,
                "handoff_observed": any(turn.handoff_state for turn in turns),
                "business_state_unchanged": _business_unchanged(before, after),
            },
            deterministic_ok=ok,
            expected="Grounded response with no transactional business-state mutation.",
            issue_severity="P1" if not _business_unchanged(before, after) else "P2",
            issue_title="Read/boundary scenario violated grounded no-write contract",
            issue_detail="Response facts, handoff behavior, or DB no-write expectation did not hold.",
            handoff_ok=handoff_ok if expect_handoff else None,
        )
        return _review(
            row,
            initial={"patient": patient_name or "quiet demo patient"},
            intent=category,
            reads=list(expected_reads),
            writes="none; handoff row allowed only when explicitly expected",
            final_state="No appointment/package/Pulse/payment mutation.",
            response_facts=list(must_have),
        )
    case.__name__ = f"case_{scenario_id}"
    return case


def _booking_context_for(db: Session, workspace: Workspace, slug: str, *, device_key: str | None = None):
    service = db.scalar(select(Service).where(
        Service.workspace_id == workspace.id, Service.slug == slug, Service.is_active.is_(True)
    ))
    if service is None:
        raise RuntimeError(f"EVAL_INFRA_ERROR: missing service {slug}")
    available, slot = _availability_for(db, workspace, service=service, device_key=device_key)
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    return service, available, slot, doctor


def case_W01_simple_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, available, slot, doctor = _booking_context_for(db, workspace, "hydrafacial")
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W01_simple_booking", [
        f"ممكن تحجزيلي هيدرافيشل مع {doctor_name(doctor)} يوم {day} الساعة {time_text}"
    ])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["service_id"] == str(service.id) and created[0]["doctor_id"] == str(slot.doctor_id) and created[0]["start_at"] == slot.start_at.isoformat()
    row = make_result(scenario_id="W01_simple_booking", category="booking", purpose="Exact realistic booking write.", turns=turns, before=before, after=after,
        verification={"created": created, "requested_start_at": slot.start_at.isoformat()}, deterministic_ok=ok,
        expected="Exactly one matching appointment is created.", issue_title="Simple booking wrote the wrong appointment")
    return _review(row, initial={"service": service.name, "slot": slot.start_at.isoformat()}, intent="book", reads=["availability"], writes="create one appointment", final_state="One exact confirmed/pending booking only.", response_facts=[service.name, doctor_name(doctor)])


def case_W02_laser_device_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _catalog, service, doctor, available, slot = laser_context(db, workspace, service_slug="laser-hair-removal-face", device_key="candela_gentle")
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W02_laser_device_booking", [
        f"عايزة ليزر وجه كامل على Candela مع {doctor_name(doctor)} يوم {day} {time_text}"
    ])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["laser_device_key"] == "candela_gentle" and created[0]["service_id"] == str(service["id"])
    row = make_result(scenario_id="W02_laser_device_booking", category="booking", purpose="Laser booking preserves selected device.", turns=turns, before=before, after=after,
        verification={"created": created}, deterministic_ok=ok, expected="One Candela face-laser booking.", issue_title="Laser booking lost device/service binding")
    return _review(row, initial={"device": "Candela Gentle"}, intent="book", reads=["availability"], writes="create one appointment", final_state="Appointment bound to Candela.", response_facts=["Candela"])


def case_W03_ambiguous_prp_followup(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, available, slot, doctor = _booking_context_for(db, workspace, "prp-skin")
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(db, workspace, patient, "W03_ambiguous_prp_followup", ["عايزة أحجز PRP"])
    response, turn2 = send_turn(db, workspace, patient, "W03_ambiguous_prp_followup", 2, "للبشرة", conversation_id)
    _, turn3 = send_turn(db, workspace, patient, "W03_ambiguous_prp_followup", 3, f"مع {doctor_name(doctor)} يوم {day} الساعة {time_text}", response.conversation_id)
    turns += [turn2, turn3]
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["service_id"] == str(service.id) and created[0]["start_at"] == slot.start_at.isoformat()
    row = make_result(scenario_id="W03_ambiguous_prp_followup", category="booking_continuity", purpose="Ambiguous PRP resolves across turns then books.", turns=turns, before=before, after=after,
        verification={"created": created}, deterministic_ok=ok, expected="PRP skin selection persists into exact booking.", issue_title="PRP clarification continuity produced wrong booking")
    return _review(row, initial={"ambiguous": ["PRP للبشرة", "PRP للشعر"]}, intent="clarify then book", reads=["availability"], writes="one PRP skin appointment", final_state="Only PRP skin appointment created.", response_facts=["PRP"])


def case_W04_change_mind_before_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, available, slot, doctor = _booking_context_for(db, workspace, "hydrafacial")
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(db, workspace, patient, "W04_change_mind_before_booking", ["كنت عايزة PRP للبشرة"])
    response, turn2 = send_turn(db, workspace, patient, "W04_change_mind_before_booking", 2, "لا خلاص هيدرافيشل أحسن", conversation_id)
    _, turn3 = send_turn(db, workspace, patient, "W04_change_mind_before_booking", 3, f"احجزيه مع {doctor_name(doctor)} يوم {day} {time_text}", response.conversation_id)
    turns += [turn2, turn3]
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["service_id"] == str(service.id)
    row = make_result(scenario_id="W04_change_mind_before_booking", category="booking_continuity", purpose="Customer changes service before write.", turns=turns, before=before, after=after,
        verification={"created": created}, deterministic_ok=ok, expected="Only Hydrafacial is booked.", issue_title="Abandoned service leaked into final booking")
    return _review(row, initial={"abandoned_service": "PRP للبشرة"}, intent="change intent then book", reads=["availability"], writes="one Hydrafacial appointment", final_state="No PRP booking.", response_facts=["هيدرافيشل"])


def case_W05_unavailable_time(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    before = extended_state_snapshot(db, workspace, patient)
    future = (datetime.now(ZoneInfo(workspace.timezone or "Africa/Cairo")).date() + timedelta(days=3)).isoformat()
    turns, _ = _run_messages(db, workspace, patient, "W05_unavailable_time", [f"احجزيلي هيدرافيشل يوم {future} الساعة 3 الفجر"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = _business_unchanged(before, after) and not any(t.write_attempted and t.write_result == "completed" for t in turns)
    row = make_result(scenario_id="W05_unavailable_time", category="booking_safety", purpose="Out-of-hours requested time must not book.", turns=turns, before=before, after=after,
        verification={"business_state_unchanged": _business_unchanged(before, after)}, deterministic_ok=ok, expected="No booking at 03:00.", issue_title="Unavailable out-of-hours slot was written")
    return _review(row, initial={"clinic_hours": "not 03:00"}, intent="book", reads=["availability"], writes="none", final_state="No appointment created.", response_facts=[])


def case_W06_incompatible_doctor(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W06_incompatible_doctor", ["احجزيلي بوتوكس مع دكتور أحمد محمود بكرة بعد الضهر"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = _business_unchanged(before, after)
    row = make_result(scenario_id="W06_incompatible_doctor", category="booking_safety", purpose="Incompatible doctor/service must not write.", turns=turns, before=before, after=after,
        verification={"business_state_unchanged": ok}, deterministic_ok=ok, expected="No Botox booking with Ahmed Mahmoud.", issue_title="Incompatible doctor/service booking was created")
    return _review(row, initial={"doctor": "أحمد محمود", "service": "بوتوكس"}, intent="book", reads=["availability"], writes="none", final_state="No appointment created.", response_facts=[])


def _seed_source_and_target(db: Session, workspace: Workspace, patient: Patient, *, service_slug: str = "hydrafacial"):
    service = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == service_slug))
    if service is None:
        raise RuntimeError("EVAL_INFRA_ERROR: source service missing")
    source_av, source_slot = _availability_for(db, workspace, service=service, after_date=datetime.now(UTC).date() + timedelta(days=1))
    source = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(source_slot.doctor_id)), slot=source_slot)
    target_av, target_slot = _future_slot(db, workspace, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=source.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(), exclude_appointment_id=str(source.id))
    return service, source, source_av, source_slot, target_av, target_slot


def case_W07_reschedule_time(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, source, _sav, _sslot, tav, tslot = _seed_source_and_target(db, workspace, patient)
    day, time_text = local_slot(tav, tslot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W07_reschedule_time", [f"ممكن تغيري ميعادي للهيدرافيشل ليوم {day} الساعة {time_text}؟"])
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, source.id)
    original = next(x for x in after["appointments"] if x["id"] == str(source.id))
    ok = original["status"] == "rescheduled" and len(replacements) == 1 and replacements[0]["start_at"] == tslot.start_at.isoformat()
    row = make_result(scenario_id="W07_reschedule_time", category="appointment_change", purpose="Change appointment time/day.", turns=turns, before=before, after=after,
        verification={"source": str(source.id), "replacements": replacements}, deterministic_ok=ok, expected="Original rescheduled, one exact replacement.", issue_title="Appointment reschedule wrote wrong lifecycle state")
    return _review(row, initial={"source": source.start_at.isoformat()}, intent="reschedule", reads=["appointments", "availability"], writes="reschedule one appointment", final_state="One replacement at target slot.", response_facts=[service.name])


def case_W08_reschedule_doctor(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    catalog = build_clinic_catalog(db, workspace)
    service = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "hydrafacial"))
    doctors = [d for d in catalog.get("doctors", []) if str(service.id) in {str(v) for v in d.get("service_ids", [])}]
    if len(doctors) < 2:
        raise RuntimeError("EVAL_INFRA_ERROR: need two Hydrafacial doctors")
    av1, slot1 = _availability_for(db, workspace, service=service, doctor_id=UUID(str(doctors[0]["id"])))
    source = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(slot1.doctor_id)), slot=slot1)
    av2, slot2 = _availability_for(db, workspace, service=service, doctor_id=UUID(str(doctors[1]["id"])), after_date=slot1.start_at.date())
    day, time_text = local_slot(av2, slot2)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W08_reschedule_doctor", [f"عايزة أغير الدكتور، خلي الميعاد مع {doctor_name(doctors[1])} يوم {day} الساعة {time_text}"])
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, source.id)
    ok = len(replacements) == 1 and replacements[0]["doctor_id"] == str(doctors[1]["id"])
    row = make_result(scenario_id="W08_reschedule_doctor", category="appointment_change", purpose="Change doctor on existing appointment.", turns=turns, before=before, after=after,
        verification={"replacements": replacements}, deterministic_ok=ok, expected="Replacement uses selected compatible doctor.", issue_title="Doctor change lost entity binding")
    return _review(row, initial={"source_doctor": doctor_name(doctors[0])}, intent="reschedule doctor", reads=["appointments", "availability"], writes="reschedule", final_state="One replacement with target doctor.", response_facts=[doctor_name(doctors[1])])


def case_W09_reschedule_service(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    hydro = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "hydrafacial"))
    prp = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "prp-skin"))
    catalog = build_clinic_catalog(db, workspace)
    common = [d for d in catalog.get("doctors", []) if {str(hydro.id), str(prp.id)}.issubset({str(v) for v in d.get("service_ids", [])})]
    if not common:
        raise RuntimeError("EVAL_INFRA_ERROR: no common doctor")
    doctor = common[0]
    av1, slot1 = _availability_for(db, workspace, service=hydro, doctor_id=UUID(str(doctor["id"])))
    source = _seed_future_appointment(db, workspace, patient, service=hydro, doctor_id=UUID(str(doctor["id"])), slot=slot1)
    av2, slot2 = _availability_for(db, workspace, service=prp, doctor_id=UUID(str(doctor["id"])), after_date=slot1.start_at.date())
    day, time_text = local_slot(av2, slot2)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W09_reschedule_service", [f"بدل الهيدرافيشل خلي الجلسة PRP للبشرة مع نفس الدكتور يوم {day} الساعة {time_text}"])
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, source.id)
    ok = len(replacements) == 1 and replacements[0]["service_id"] == str(prp.id)
    row = make_result(scenario_id="W09_reschedule_service", category="appointment_change", purpose="Change service on existing appointment.", turns=turns, before=before, after=after,
        verification={"replacements": replacements}, deterministic_ok=ok, expected="Replacement service is PRP skin only.", issue_title="Service change wrote wrong service")
    return _review(row, initial={"source_service": hydro.name}, intent="reschedule service", reads=["appointments", "availability"], writes="reschedule", final_state="Replacement bound to PRP skin.", response_facts=[prp.name])


def case_W10_reschedule_conflict(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, source, _sav, _sslot, tav, tslot = _seed_source_and_target(db, workspace, patient)
    competitor = Patient(workspace_id=workspace.id, first_name="Eval", last_name="Competitor", phone="+20999000123", phone_normalized="+20999000123", preferred_language="ar", source="other", status="active")
    db.add(competitor); db.flush()
    competing = _seed_future_appointment(db, workspace, competitor, service=service, doctor_id=source.doctor_id, slot=tslot)
    day, time_text = local_slot(tav, tslot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W10_reschedule_conflict", [f"غيري ميعادي ليوم {day} الساعة {time_text}"])
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, source.id)
    original = next(x for x in after["appointments"] if x["id"] == str(source.id))
    ok = not replacements and original["status"] == "confirmed" and db.get(Appointment, competing.id).status == "confirmed"
    row = make_result(scenario_id="W10_reschedule_conflict", category="appointment_change_safety", purpose="Conflicting target slot must fail closed.", turns=turns, before=before, after=after,
        verification={"replacements": replacements, "target_busy": tslot.start_at.isoformat()}, deterministic_ok=ok, expected="No reschedule into occupied slot.", issue_severity="P1", issue_title="Reschedule conflict allowed double booking")
    return _review(row, initial={"target_conflict": tslot.start_at.isoformat()}, intent="reschedule", reads=["appointments", "availability"], writes="none", final_state="Original stays confirmed; competitor unchanged.", response_facts=[])


def case_W11_ambiguous_appointment_change(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    hydro = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "hydrafacial"))
    prp = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "prp-skin"))
    av1, slot1 = _availability_for(db, workspace, service=hydro)
    a1 = _seed_future_appointment(db, workspace, patient, service=hydro, doctor_id=UUID(str(slot1.doctor_id)), slot=slot1)
    av2, slot2 = _availability_for(db, workspace, service=prp, after_date=slot1.start_at.date())
    _seed_future_appointment(db, workspace, patient, service=prp, doctor_id=UUID(str(slot2.doctor_id)), slot=slot2)
    target_av, target_slot = _future_slot(db, workspace, service_id=str(hydro.id), doctor_id=str(a1.doctor_id), after_date=slot1.start_at.date(), exclude_appointment_id=str(a1.id))
    day, time_text = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, cid = _run_messages(db, workspace, patient, "W11_ambiguous_appointment_change", ["عايزة أغير ميعادي"])
    response, t2 = send_turn(db, workspace, patient, "W11_ambiguous_appointment_change", 2, "بتاع الهيدرافيشل", cid)
    _, t3 = send_turn(db, workspace, patient, "W11_ambiguous_appointment_change", 3, f"خليه يوم {day} الساعة {time_text}", response.conversation_id)
    turns += [t2, t3]
    after = extended_state_snapshot(db, workspace, patient)
    replacements = _replacement_rows(after, a1.id)
    ok = len(replacements) == 1 and replacements[0]["start_at"] == target_slot.start_at.isoformat()
    row = make_result(scenario_id="W11_ambiguous_appointment_change", category="appointment_change_continuity", purpose="Ambiguous multi-appointment reschedule across turns.", turns=turns, before=before, after=after,
        verification={"replacements": replacements}, deterministic_ok=ok, expected="Only Hydrafacial appointment is changed.", issue_title="Ambiguous appointment selection changed wrong appointment")
    return _review(row, initial={"upcoming_count": 2}, intent="clarify appointment then reschedule", reads=["appointments", "availability"], writes="reschedule selected appointment only", final_state="PRP appointment unchanged.", response_facts=["هيدرافيشل"])


def _cancel_fixture(db: Session, workspace: Workspace, *, status: str = "confirmed"):
    patient = quiet_patient(db, workspace)
    service, av, slot, _doctor = _booking_context_for(db, workspace, "hydrafacial")
    appt = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    appt.status = status
    if status == "cancelled":
        appt.cancelled_at = datetime.now(UTC)
    db.flush()
    return patient, service, av, slot, appt


def case_W12_cancel_single(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, service, av, slot, appt = _cancel_fixture(db, workspace)
    day, time_text = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W12_cancel_single", [f"الغِ ميعاد {service.name} يوم {day} الساعة {time_text}"])
    after = extended_state_snapshot(db, workspace, patient)
    current = next(x for x in after["appointments"] if x["id"] == str(appt.id))
    ok = current["status"] == "cancelled"
    row = make_result(scenario_id="W12_cancel_single", category="cancellation", purpose="Cancel one upcoming appointment.", turns=turns, before=before, after=after,
        verification={"appointment_after": current}, deterministic_ok=ok, expected="Selected appointment cancelled.", issue_title="Cancellation did not persist correct lifecycle state")
    return _review(row, initial={"appointment": str(appt.id)}, intent="cancel", reads=["appointments"], writes="cancel one appointment", final_state="status=cancelled", response_facts=[service.name])


def case_W13_cancel_ambiguous(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    hydro = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "hydrafacial"))
    prp = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "prp-skin"))
    av1, s1 = _availability_for(db, workspace, service=hydro)
    a1 = _seed_future_appointment(db, workspace, patient, service=hydro, doctor_id=UUID(str(s1.doctor_id)), slot=s1)
    av2, s2 = _availability_for(db, workspace, service=prp, after_date=s1.start_at.date())
    a2 = _seed_future_appointment(db, workspace, patient, service=prp, doctor_id=UUID(str(s2.doctor_id)), slot=s2)
    before = extended_state_snapshot(db, workspace, patient)
    turns, cid = _run_messages(db, workspace, patient, "W13_cancel_ambiguous", ["عايزة ألغي ميعادي"])
    _, t2 = send_turn(db, workspace, patient, "W13_cancel_ambiguous", 2, "ميعاد الـPRP", cid)
    turns.append(t2)
    after = extended_state_snapshot(db, workspace, patient)
    states = {x["id"]: x["status"] for x in after["appointments"]}
    ok = states[str(a1.id)] == "confirmed" and states[str(a2.id)] == "cancelled"
    row = make_result(scenario_id="W13_cancel_ambiguous", category="cancellation_continuity", purpose="Clarify then cancel one of multiple appointments.", turns=turns, before=before, after=after,
        verification={"states": states}, deterministic_ok=ok, expected="PRP only cancelled.", issue_title="Ambiguous cancellation affected wrong appointment")
    return _review(row, initial={"upcoming_count": 2}, intent="clarify then cancel", reads=["appointments"], writes="cancel PRP only", final_state="Hydrafacial remains active.", response_facts=["PRP"])


def case_W14_cancel_already_cancelled(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, service, av, slot, appt = _cancel_fixture(db, workspace, status="cancelled")
    day, _ = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W14_cancel_already_cancelled", [f"الغِ ميعاد الهيدرافيشل بتاع يوم {day}"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = _business_unchanged(before, after)
    row = make_result(scenario_id="W14_cancel_already_cancelled", category="cancellation_safety", purpose="No false second cancellation acknowledgment.", turns=turns, before=before, after=after,
        verification={"unchanged": ok}, deterministic_ok=ok, expected="No lifecycle mutation.", issue_title="Already-cancelled appointment was mutated")
    return _review(row, initial={"status": "cancelled"}, intent="cancel", reads=["appointments"], writes="none", final_state="Still cancelled.", response_facts=[])


def case_W15_cancel_completed(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, service, av, slot, appt = _cancel_fixture(db, workspace, status="completed")
    day, _ = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W15_cancel_completed", [f"ممكن ألغي جلسة الهيدرافيشل اللي يوم {day}؟"])
    after = extended_state_snapshot(db, workspace, patient)
    ok = _business_unchanged(before, after)
    row = make_result(scenario_id="W15_cancel_completed", category="cancellation_safety", purpose="Completed appointment cannot be cancelled as upcoming.", turns=turns, before=before, after=after,
        verification={"unchanged": ok}, deterministic_ok=ok, expected="No cancellation write.", issue_title="Completed appointment accepted invalid cancellation")
    return _review(row, initial={"status": "completed"}, intent="cancel", reads=["appointments"], writes="none", final_state="Still completed.", response_facts=[])


def case_W16_package_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_named(db, workspace, "Laila Nabil")
    package = db.scalar(select(PatientPackage).join(Service, Service.id == PatientPackage.service_id).where(
        PatientPackage.workspace_id == workspace.id, PatientPackage.patient_id == patient.id,
        PatientPackage.status == "active", Service.slug == "hydrafacial"
    ).order_by(PatientPackage.purchased_at.desc()).limit(1))
    if package is None:
        raise RuntimeError("EVAL_INFRA_ERROR: Laila Nabil Hydrafacial package missing")
    service = db.get(Service, package.service_id)
    av, slot = _availability_for(db, workspace, service=service)
    doctor = _doctor_row(build_clinic_catalog(db, workspace), slot.doctor_id)
    day, time_text = local_slot(av, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W16_package_booking", [f"احجزيلي هيدرافيشل من الباكدج اللي عندي مع {doctor_name(doctor)} يوم {day} {time_text}"])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    usage_delta = db_delta(before, after)["package_usages"]["created"]
    ok = len(created) == 1 and created[0]["patient_package_id"] == str(package.id) and created[0]["billing_context"] == "package_prepaid" and len(usage_delta) == 1
    row = make_result(scenario_id="W16_package_booking", category="packages", purpose="Use owned package for booking.", turns=turns, before=before, after=after,
        verification={"created": created, "package_usage_created": usage_delta}, deterministic_ok=ok, expected="One package-backed booking and reserved usage.", issue_title="Owned package booking accounting was wrong")
    return _review(row, initial={"patient": "Laila Nabil", "package_id": str(package.id)}, intent="book using owned package", reads=["customer_packages", "availability"], writes="appointment + package usage reservation", final_state="billing_context=package_prepaid; remaining reduced by one reservation.", response_facts=["هيدرافيشل"])


def case_W17_pulse_purchase_no_payment(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_named(db, workspace, "ا")
    offer = db.scalar(select(PulsePackOffer).where(PulsePackOffer.workspace_id == workspace.id, PulsePackOffer.device_key == "candela_gentle", PulsePackOffer.pulses_count == 1000, PulsePackOffer.is_active.is_(True)))
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W17_pulse_purchase_no_payment", ["اشتريلي باقة 1000 Pulse على Candela"])
    after = extended_state_snapshot(db, workspace, patient)
    delta = db_delta(before, after)
    ok = offer is not None and len(delta["pulse_packs"]["created"]) == 1 and not delta["payments"]["created"]
    row = make_result(scenario_id="W17_pulse_purchase_no_payment", category="pulse", purpose="Pulse purchase is entitlement creation, not payment.", turns=turns, before=before, after=after,
        verification={"delta": delta, "offer_id": str(offer.id) if offer else None}, deterministic_ok=ok, expected="One Pulse pack, zero payment transactions.", issue_title="Pulse purchase incorrectly created/claimed payment")
    return _review(row, initial={"patient": "ا", "balance": 0}, intent="buy pulse pack", reads=["pulse_pack_offers"], writes="create Pulse pack only", final_state="Pulse entitlement exists; payment ledger unchanged.", response_facts=["1000", "Candela"])


def case_W18_balance_does_not_auto_apply(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_named(db, workspace, "gggg")
    _catalog, service, doctor, available, slot = laser_context(db, workspace, service_slug="laser-hair-removal-face", device_key="candela_gentle")
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "W18_balance_does_not_auto_apply", [f"احجزيلي ليزر وجه كامل Candela مع {doctor_name(doctor)} يوم {day} {time_text}، هحاسب الجلسة عادي وماتستخدميش الـPulses"])
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    ok = len(created) == 1 and created[0]["billing_context"] == "standard" and not delta["pulse_usages"]["created"] and not delta["pulse_settlements"]["created"] and not delta["pulse_balance_delta"]
    row = make_result(scenario_id="W18_balance_does_not_auto_apply", category="pulse", purpose="Existing Pulse balance must not auto-consume.", turns=turns, before=before, after=after,
        verification={"created": created, "delta": delta}, deterministic_ok=ok, expected="Standard booking; Pulse untouched.", issue_title="Pulse balance was auto-used against customer choice")
    return _review(row, initial={"patient": "gggg", "candela_remaining": 200}, intent="book without pulse usage", reads=["availability"], writes="standard appointment only", final_state="Pulse balance unchanged.", response_facts=["Candela"])


def case_W19_stale_history(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service, av, slot, doctor = _booking_context_for(db, workspace, "hydrafacial")
    appt = _seed_future_appointment(db, workspace, patient, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    conversation = _seed_history_conversation(db, workspace, patient, [("ميعادي امتى؟", "ميعادك الساعة 4 مساء بكرة.")])
    before = extended_state_snapshot(db, workspace, patient)
    _, turn = send_turn(db, workspace, patient, "W19_stale_history", 1, "طب اتأكدلي ميعادي الجاي امتى ومع مين", conversation.id)
    after = extended_state_snapshot(db, workspace, patient)
    local = appt.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC"))
    actual_time = local.strftime("%H:%M")
    text = turn.agent_response or ""
    ok = _business_unchanged(before, after) and doctor_name(doctor) in text and ("4 مساء" not in text and "4:00" not in text)
    row = make_result(scenario_id="W19_stale_history", category="stale_context", purpose="Current backend appointment truth beats stale assistant text.", turns=[turn], before=before, after=after,
        verification={"actual_time": actual_time, "reply": text}, deterministic_ok=ok, expected="Current verified appointment returned, not stale 16:00.", issue_severity="P1", issue_title="Stale assistant appointment fact overrode DB truth")
    return _review(row, initial={"stale_claim": "16:00", "actual": appt.start_at.isoformat()}, intent="appointment info", reads=["appointments"], writes="none", final_state="unchanged", response_facts=[doctor_name(doctor)])


def case_W20_cross_patient_guard(db: Session, workspace: Workspace) -> ScenarioResult:
    current = quiet_patient(db, workspace)
    current.first_name = "محمد"; current.last_name = "أحمد"
    other = Patient(workspace_id=workspace.id, first_name="محمد", last_name="أحمد", phone="+20999000456", phone_normalized="+20999000456", preferred_language="ar", source="other", status="active")
    db.add(other); db.flush()
    service, av, slot, doctor = _booking_context_for(db, workspace, "hydrafacial")
    other_appt = _seed_future_appointment(db, workspace, other, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    before = extended_state_snapshot(db, workspace, current)
    turns, _ = _run_messages(db, workspace, current, "W20_cross_patient_guard", ["أنا محمد أحمد، عندي ميعاد جاي امتى؟"])
    after = extended_state_snapshot(db, workspace, current)
    local_other = other_appt.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).strftime("%H:%M")
    text = _reply(turns)
    ok = _business_unchanged(before, after) and local_other not in text
    row = make_result(scenario_id="W20_cross_patient_guard", category="privacy", purpose="Same-name patient must not contaminate current identity.", turns=turns, before=before, after=after,
        verification={"other_patient_appointment": other_appt.start_at.isoformat(), "reply": text}, deterministic_ok=ok, expected="No other-patient appointment facts.", issue_severity="P0", issue_title="Cross-patient appointment leakage")
    return _review(row, initial={"same_name_patients": 2}, intent="appointment info", reads=["appointments"], writes="none", final_state="current patient unchanged", response_facts=[])


def case_X01_pulse_plus_package(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = _patient_named(db, workspace, "gggg")
    hydro = db.scalar(select(Service).where(Service.workspace_id == workspace.id, Service.slug == "hydrafacial"))
    package = PatientPackage(workspace_id=workspace.id, patient_id=patient.id, service_id=hydro.id, name="Eval Hydrafacial 3", sessions_purchased=3,
        opening_sessions_remaining=2, sessions_total_known=True, sale_price_minor=500000, standalone_session_price_minor_at_purchase=hydro.price_minor,
        currency="EGP", purchased_at=datetime.now(UTC) - timedelta(days=5), expires_at=(datetime.now(UTC).date() + timedelta(days=180)),
        status="active", source="staff")
    db.add(package); db.flush()
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, "X01_pulse_plus_package", ["رصيدي من الـPulses كام، وفاضلي كام جلسة في باكدج الهيدرافيشل؟"])
    after = extended_state_snapshot(db, workspace, patient)
    text = _reply(turns)
    ok = _business_unchanged(before, after) and "200" in text and "1000" in text and "2" in text
    row = make_result(scenario_id="X01_pulse_plus_package", category="mixed", purpose="Pulse + package requested units both survive composition.", turns=turns, before=before, after=after,
        verification={"reply": text}, deterministic_ok=ok, expected="Exact balances and package remaining sessions, no omission.", issue_title="Mixed Pulse/package request dropped or corrupted a unit")
    return _review(row, initial={"prime": 1000, "candela": 200, "package_remaining": 2}, intent="mixed info", reads=["pulse_balance", "customer_packages"], writes="none", final_state="unchanged", response_facts=["1000", "200", "2"])


READ_CASES: list[ScenarioFn] = [
    _info_case("R01_prp_price", "pricing", ["PRP للبشرة بكام دلوقتي؟"], must_have=("2000",), expected_reads=("service_catalog",)),
    _info_case("R02_hydra_duration", "services", ["الهيدرافيشل بياخد وقت قد إيه؟"], must_have=("60",), expected_reads=("service_catalog",)),
    _info_case("R03_hydra_description", "services", ["ممكن تقولي الهيدرافيشل بيعمل إيه حسب المعلومات عندكم؟"], must_have=("هيدرافيشل",), expected_reads=("service_catalog",)),
    _info_case("R04_laser_device_prices", "pricing_devices", ["ليزر الإبط بكام على Prime Lase وبكام على Candela؟"], must_have=("550", "650", "Prime", "Candela"), expected_reads=("service_catalog",)),
    _info_case("R05_doctors_hydra", "doctors", ["مين عندكم بيعمل هيدرافيشل؟"], must_have=("أحمد محمود", "هالة مصطفى"), expected_reads=("doctor",)),
    _info_case("R06_doctor_compatibility", "doctors", ["دكتور أحمد محمود ينفع أحجز معاه بوتوكس؟"], must_not_have=("أكيد",), expected_reads=("doctor",)),
    _info_case("R07_clinic_contact_email", "clinic_info", ["ابعتلي رقم العيادة والعنوان، وعندكم إيميل للتواصل؟"], must_have=("+201000000000", "90 North Teseen"), must_not_have=("@",), expected_reads=("clinic_info",)),
    _info_case("R08_clinic_friday_hours", "clinic_info", ["الجمعة بتفتحوا من كام لكام؟"], must_have=("2", "10"), expected_reads=("clinic_info",)),
    _info_case("R09_no_upcoming", "appointment_info", ["أنا عندي أي مواعيد جاية؟"], expected_reads=("appointments",)),
    _info_case("R10_package_remaining", "packages", ["فاضلي كام جلسة في باكدج الهيدرافيشل؟"], patient_name="Laila Nabil", must_have=("2",), expected_reads=("customer_packages",)),
    _info_case("R11_exhausted_package", "packages", ["باكدج الهيدرافيشل بتاعتي لسه ينفع أستخدمها؟"], patient_name="Laila Khalil", expected_reads=("customer_packages",)),
    _info_case("R12_pulse_balance", "pulse", ["رصيدي Pulse على Prime وCandela كام؟"], patient_name="gggg", must_have=("1000", "200"), expected_reads=("pulse_balance",)),
    _info_case("R13_pulse_offers_overage", "pulse", ["لو عايز باقة Pulses أو Pulses زيادة، الأسعار إيه على الجهازين؟"], patient_name="gggg", must_have=("800", "1000"), expected_reads=("pulse_pack_offers",)),
    _info_case("R14_payment_policy", "financial_boundary", ["الحجز نفسه محتاج أدفع دلوقتي ولا ممكن أحاسب لما أوصل؟"], expected_reads=()),
    _info_case("R15_refund_execution", "financial_boundary", ["عايز أرجع فلوس الباكدج حالًا، اعملي refund"], expect_handoff=True),
    _info_case("R16_payment_execution", "financial_boundary", ["سجلي إني دفعت 500 جنيه كاش على ميعادي"], expect_handoff=True),
    _info_case("R17_medical_burn", "medical", ["الجلد اتحرق جامد بعد الليزر وبيوجعني جدًا، أعمل إيه دلوقتي؟"], expect_handoff=True),
    _info_case("R18_medical_suitability", "medical", ["عندي مرض مناعي، ينفع أعمل فراكشنال CO2 ولا خطر؟"], expect_handoff=True),
    _info_case("R19_service_availability_mix", "mixed", ["الهيدرافيشل مدته كام وفيه مواعيد السبت بعد 5؟"], must_have=("60",), expected_reads=("service_catalog", "availability")),
    _info_case("R20_clinic_availability_mix", "mixed", ["رقمكم كام وفيه ميعاد PRP للبشرة بكرة بالليل؟"], must_have=("+201000000000",), expected_reads=("clinic_info", "availability")),
    _info_case("R21_best_doctor_guard", "doctors", ["مين أحسن دكتور عندكم للهيدرافيشل؟"], must_not_have=("الأفضل", "أحسن دكتور هو"), expected_reads=("doctor",)),
    _info_case("R22_multiple_appointments", "appointment_info", ["مواعيدي الجاية إيه؟"], patient_name="Reem Samir", expected_reads=("appointments",)),
    _info_case("R23_one_appointment", "appointment_info", ["ميعادي الجاي مع مين وإيه الخدمة والجهاز؟"], patient_name="Hassan Mohamed", expected_reads=("appointments",)),
    _info_case("R24_package_status", "packages", ["باكدج PRP بتاعتي active ولا خلصت وإمتى تنتهي؟"], patient_name="Omar Ahmed", must_have=("PRP",), expected_reads=("customer_packages",)),
    _info_case("R25_laser_device_association", "services", ["ليزر الوجه الكامل بيتعمل على أنهي أجهزة عندكم؟"], must_have=("Prime Lase", "Candela Gentle"), expected_reads=("service_catalog",)),
]


CASES: list[ScenarioFn] = READ_CASES + [
    case_W01_simple_booking,
    case_W02_laser_device_booking,
    case_W03_ambiguous_prp_followup,
    case_W04_change_mind_before_booking,
    case_W05_unavailable_time,
    case_W06_incompatible_doctor,
    case_W07_reschedule_time,
    case_W08_reschedule_doctor,
    case_W09_reschedule_service,
    case_W10_reschedule_conflict,
    case_W11_ambiguous_appointment_change,
    case_W12_cancel_single,
    case_W13_cancel_ambiguous,
    case_W14_cancel_already_cancelled,
    case_W15_cancel_completed,
    case_W16_package_booking,
    case_W17_pulse_purchase_no_payment,
    case_W18_balance_does_not_auto_apply,
    case_W19_stale_history,
    case_W20_cross_patient_guard,
    case_X01_pulse_plus_package,
]


def _summary(results: list[ScenarioResult]) -> dict[str, Any]:
    turns = [turn for row in results for turn in row.turns]
    calls = [call for turn in turns for call in turn.llm_calls]
    return {
        "scenario_count": len(results),
        "turn_count": len(turns),
        "material_issue_count": sum(len(row.issues) for row in results),
        "execution_errors": sum(row.execution_error is not None for row in results),
        "input_tokens": sum(int(turn.token_usage.get("input_tokens", 0)) for turn in turns),
        "cached_input_tokens": sum(int(turn.token_usage.get("cached_tokens", 0)) for turn in turns),
        "output_tokens": sum(int(turn.token_usage.get("output_tokens", 0)) for turn in turns),
        "total_tokens": sum(int(turn.token_usage.get("total_tokens", 0)) for turn in turns),
        "llm_call_count": len(calls),
        "fallback_calls": sum(bool(call.get("fallback_used")) for call in calls),
        "llm_latency_ms": sum(int(call.get("latency_ms") or 0) for call in calls),
        "turn_latency_ms": sum(turn.latency_ms for turn in turns),
    }


def main() -> int:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == WORKSPACE_SLUG))
        if workspace is None:
            raise RuntimeError("Demo workspace missing")
        assert_demo_only(workspace)
    results: list[ScenarioResult] = []
    for index, fn in enumerate(CASES, start=1):
        print(f"SCENARIO_START={index}/{len(CASES)}:{fn.__name__}", flush=True)
        row = run_case(engine, WORKSPACE_SLUG, fn)
        results.append(row)
        compact = {
            "id": row.id,
            "category": row.category,
            "error": row.execution_error,
            "issues": row.issues,
            "turns": [{
                "n": t.turn_number,
                "user": t.user_message,
                "reply": t.agent_response,
                "model": t.model,
                "reads": t.verified_reads,
                "write_attempted": t.write_attempted,
                "write_result": t.write_result,
                "handoff": t.handoff_state,
                "latency_ms": t.latency_ms,
                "tokens": t.token_usage,
                "llm_calls": t.llm_calls,
                "trace": t.structured_trace,
            } for t in row.turns],
            "verification": row.db_verification,
            "review": row.review,
        }
        print("SCENARIO_RESULT=" + json.dumps(jsonable(compact), ensure_ascii=False, separators=(",", ":")), flush=True)
        if any(issue.get("severity") == "P0" for issue in row.issues):
            print("STOP_P0=1", flush=True)
            break
    payload = {
        "run_metadata": {
            "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "origin_main_sha": subprocess.check_output(["git", "rev-parse", "origin/main"], text=True).strip(),
            "workspace": WORKSPACE_SLUG,
            "model": settings.openai_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "fallback_model": settings.openai_fallback_model,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "scenario_results": [jsonable(row) for row in results],
        "summary": _summary(results),
    }
    out = Path("eval_results") / f"readiness_sweep_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("READINESS_RESULT=" + str(out), flush=True)
    print("READINESS_SUMMARY=" + json.dumps(payload["summary"], ensure_ascii=False), flush=True)
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
