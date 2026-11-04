from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.clinic_inventory import ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.service import Service
from app.models.working_hours import DoctorAvailabilityWindow, DoctorWorkingHour
from app.models.workspace import Workspace
from app.services import booking as booking_module
from app.services.agent_v2 import live_chat as live_chat_module
from app.services.booking import get_effective_booking_settings
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    active_branch_id,
    assert_demo_only,
    jsonable,
    local_slot,
    send_turn,
    service_by_slug,
)
from tools.agent_eval.run_batch_01 import (
    created_appointments,
    doctor_name,
    quiet_patient,
)
from tools.agent_eval.run_batch_02 import (
    _apply_cost,
    _ensure_batch2_catalog_fixtures,
    _seed_history_conversation,
    laser_context,
)
from tools.agent_eval.run_batch_03 import (
    _appointment_by_id,
    _future_slot,
    _run_messages,
    _seed_future_appointment,
    db_delta,
    extended_state_snapshot,
    make_result,
    summarize,
)
from tools.agent_eval.run_batch_03 import run_case as _run_case
from tools.agent_eval.run_batch_04 import (
    _availability_for,
    _business_delta_empty,
    _doctor_row,
    _replacement_rows,
)

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
BATCH_NUMBER = 5
SCENARIO_VERSION = "batch5-v1"
BATCH5_FIXTURE_VERSION = "batch5-demo-fixtures-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--output-dir", default="backend/eval_results")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA") or "unknown")
    parser.add_argument("--batch5-base-sha", required=True)
    parser.add_argument("--input-price-per-million", type=float, required=True)
    parser.add_argument("--cached-input-price-per-million", type=float, required=True)
    parser.add_argument("--output-price-per-million", type=float, required=True)
    parser.add_argument("--cache-write-multiplier", type=float, default=1.0)
    parser.add_argument("--pricing-source", required=True)
    return parser.parse_args()
def require_explicit_demo_eval() -> None:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1 to run the live Demo evaluation.")


def _new_patient(
    db: Session,
    workspace: Workspace,
    *,
    first_name: str,
    last_name: str,
) -> Patient:
    suffix = str(uuid4().int % 1_000_000_000).zfill(9)
    phone = f"+209{suffix}"
    patient = Patient(
        workspace_id=workspace.id,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        phone_normalized=phone,
        preferred_language="ar",
        source="other",
        status="active",
    )
    db.add(patient)
    db.flush()
    return patient


def _patient_appointments(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Appointment)
            .where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
            .order_by(Appointment.start_at, Appointment.id)
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
            "rescheduled_from_appointment_id": (
                str(row.rescheduled_from_appointment_id)
                if row.rescheduled_from_appointment_id
                else None
            ),
        }
        for row in rows
    ]


def _current_and_other_same_name(
    db: Session,
    workspace: Workspace,
) -> tuple[Patient, Patient]:
    current = quiet_patient(db, workspace)
    current.first_name = "محمد"
    current.last_name = "أحمد"
    if not current.phone_normalized:
        suffix = str(uuid4().int % 1_000_000_000).zfill(9)
        current.phone = f"+208{suffix}"
        current.phone_normalized = current.phone
    other = _new_patient(
        db,
        workspace,
        first_name="محمد",
        last_name="أحمد",
    )
    db.flush()
    return current, other


def _regular_doctor_slot(
    db: Session,
    workspace: Workspace,
    service: Service,
):
    catalog = build_clinic_catalog(db, workspace)
    for doctor in catalog.get("doctors", []):
        if not isinstance(doctor, dict) or not doctor.get("id"):
            continue
        if str(service.id) not in {str(value) for value in (doctor.get("service_ids") or [])}:
            continue
        doctor_model = db.get(Doctor, UUID(str(doctor["id"])))
        if doctor_model is None or doctor_model.doctor_type != "regular":
            continue
        try:
            available, slot = _availability_for(
                db,
                workspace,
                service=service,
                doctor_id=doctor_model.id,
                after_date=datetime.now(UTC).date() + timedelta(days=1),
            )
        except RuntimeError:
            continue
        return doctor, doctor_model, available, slot
    raise RuntimeError("EVAL_INFRA_ERROR: no regular doctor with future slot")


def _valid_device_keys(
    db: Session,
    workspace: Workspace,
    service: Service,
) -> set[str]:
    return {
        str(value)
        for value in db.scalars(
            select(ServiceDevicePrice.device_key).where(
                ServiceDevicePrice.workspace_id == workspace.id,
                ServiceDevicePrice.service_id == service.id,
                ServiceDevicePrice.is_active.is_(True),
            )
        )
        if value
    }


def _wrong_created_for_target(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    expected_doctor_id: UUID | str | None = None,
    expected_service_id: UUID | str | None = None,
    expected_device_key: str | None = None,
) -> list[dict[str, Any]]:
    rows = created_appointments(before, after)
    wrong: list[dict[str, Any]] = []
    for row in rows:
        if expected_doctor_id is not None and row["doctor_id"] != str(expected_doctor_id):
            wrong.append(row)
            continue
        if expected_service_id is not None and row["service_id"] != str(expected_service_id):
            wrong.append(row)
            continue
        if expected_device_key is not None and row["laser_device_key"] != expected_device_key:
            wrong.append(row)
    return wrong
def case_01_same_name_different_patients(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    other_a = _new_patient(db, workspace, first_name="محمد", last_name="أحمد")
    other_b = _new_patient(db, workspace, first_name="محمد", last_name="أحمد")
    service = service_by_slug(db, workspace, "hydrafacial")
    av_a, slot_a = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    apt_a = _seed_future_appointment(
        db, workspace, other_a, service=service,
        doctor_id=UUID(str(slot_a.doctor_id)), slot=slot_a,
    )
    av_b, slot_b = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    apt_b = _seed_future_appointment(
        db, workspace, other_b, service=service,
        doctor_id=UUID(str(slot_b.doctor_id)), slot=slot_b,
    )
    other_before = {
        str(other_a.id): _patient_appointments(db, workspace, other_a),
        str(other_b.id): _patient_appointments(db, workspace, other_b),
    }
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b5_01_same_name_different_patients",
        ["غيري ميعاد محمد أحمد"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    other_after = {
        str(other_a.id): _patient_appointments(db, workspace, other_a),
        str(other_b.id): _patient_appointments(db, workspace, other_b),
    }
    cross_patient_mutation = other_before != other_after
    delta = db_delta(before, after)
    ok = not cross_patient_mutation and _business_delta_empty(delta)
    return make_result(
        scenario_id="b5_01_same_name_different_patients",
        category="patient_identity",
        purpose="Ambiguous same-name third-party patients must never be guessed from customer text.",
        turns=turns, before=before, after=after,
        verification={
            "ambiguous_patient_ids": [str(other_a.id), str(other_b.id)],
            "ambiguous_appointment_ids": [str(apt_a.id), str(apt_b.id)],
            "other_before": other_before,
            "other_after": other_after,
            "availability_dates": [local_slot(av_a, slot_a)[0], local_slot(av_b, slot_b)[0]],
            "cross_patient_mutation": cross_patient_mutation,
        },
        deterministic_ok=ok,
        expected="No cross-patient read-derived mutation; clarify or stay scoped to the verified current customer.",
        issue_severity="P0" if cross_patient_mutation else "P2",
        issue_title="Ambiguous same-name patient resolution crossed the current-customer boundary",
    )
def case_02_phone_beats_ambiguous_name(db: Session, workspace: Workspace) -> ScenarioResult:
    patient, other = _current_and_other_same_name(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _source_av, source_slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    source = _seed_future_appointment(
        db, workspace, patient, service=service,
        doctor_id=UUID(str(source_slot.doctor_id)), slot=source_slot,
    )
    _other_av, other_slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=3),
    )
    other_appointment = _seed_future_appointment(
        db, workspace, other, service=service,
        doctor_id=UUID(str(other_slot.doctor_id)), slot=other_slot,
    )
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=source.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(source.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    other_before = _patient_appointments(db, workspace, other)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b5_02_phone_beats_ambiguous_name",
        [f"أنا محمد أحمد، غيري ميعادي وخليه يوم {target_date} الساعة {target_time}"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    other_after = _patient_appointments(db, workspace, other)
    replacements = _replacement_rows(after, source.id)
    current_ok = (
        _appointment_by_id(after, source.id)["status"] == "rescheduled"
        and len(replacements) == 1
        and replacements[0]["start_at"] == target_slot.start_at.isoformat()
    )
    wrong_other = other_before != other_after
    ok = current_ok and not wrong_other
    return make_result(
        scenario_id="b5_02_phone_beats_ambiguous_name",
        category="patient_identity",
        purpose="Verified current-customer identity must beat an ambiguous duplicated name.",
        turns=turns, before=before, after=after,
        verification={
            "current_patient_id": str(patient.id),
            "current_phone": patient.phone_normalized,
            "other_patient_id": str(other.id),
            "other_appointment_id": str(other_appointment.id),
            "other_unchanged": not wrong_other,
            "replacements": replacements,
        },
        deterministic_ok=ok,
        expected="Only the verified current customer's appointment is rescheduled; the same-name patient is untouched.",
        issue_severity="P0" if wrong_other else ("P1" if turns[-1].write_attempted else "P2"),
        issue_title="Ambiguous name overrode verified customer identity",
    )


def case_03_historical_name_not_current_identity(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    patient.first_name = "سارة"
    patient.last_name = "علي"
    other = _new_patient(db, workspace, first_name="محمود", last_name="حسن")
    db.flush()
    service = service_by_slug(db, workspace, "hydrafacial")
    _source_av, source_slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    source = _seed_future_appointment(
        db, workspace, patient, service=service,
        doctor_id=UUID(str(source_slot.doctor_id)), slot=source_slot,
    )
    _other_av, other_slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=3),
    )
    other_appointment = _seed_future_appointment(
        db, workspace, other, service=service,
        doctor_id=UUID(str(other_slot.doctor_id)), slot=other_slot,
    )
    conversation = _seed_history_conversation(
        db,
        workspace,
        patient,
        [("أنا محمود حسن وعايز أغير ميعادي", "تمام، قولّي الميعاد الجديد.")],
    )
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=source.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(source.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    other_before = _patient_appointments(db, workspace, other)
    before = extended_state_snapshot(db, workspace, patient)
    _, turn = send_turn(
        db, workspace, patient, "b5_03_historical_name_not_current_identity", 1,
        f"أنا سارة علي، خليه يوم {target_date} الساعة {target_time}", conversation.id,
    )
    after = extended_state_snapshot(db, workspace, patient)
    other_after = _patient_appointments(db, workspace, other)
    replacements = _replacement_rows(after, source.id)
    wrong_other = other_before != other_after
    current_ok = (
        _appointment_by_id(after, source.id)["status"] == "rescheduled"
        and len(replacements) == 1
        and replacements[0]["start_at"] == target_slot.start_at.isoformat()
    )
    ok = current_ok and not wrong_other
    return make_result(
        scenario_id="b5_03_historical_name_not_current_identity",
        category="patient_identity",
        purpose="A stale historical patient name must not override the verified current customer.",
        turns=[turn], before=before, after=after,
        verification={
            "current_patient_id": str(patient.id),
            "historical_name": "محمود حسن",
            "other_patient_id": str(other.id),
            "other_appointment_id": str(other_appointment.id),
            "other_unchanged": not wrong_other,
            "replacements": replacements,
        },
        deterministic_ok=ok,
        expected="Canonical current identity wins; no cross-patient read/write occurs.",
        issue_severity="P0" if wrong_other else ("P1" if turn.write_attempted else "P2"),
        issue_title="Stale historical identity contaminated the current customer lifecycle",
    )
def case_04_slot_becomes_unavailable(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    available, slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, time_text = local_slot(available, slot)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b5_04_slot_becomes_unavailable",
        [f"هل {service.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor)} متاح؟"],
    )
    competitor = _new_patient(db, workspace, first_name="عميل", last_name="منافس")
    competing = _seed_future_appointment(
        db, workspace, competitor, service=service,
        doctor_id=UUID(str(slot.doctor_id)), slot=slot,
    )
    before_booking = extended_state_snapshot(db, workspace, patient)
    _, booking_turn = send_turn(
        db, workspace, patient, "b5_04_slot_becomes_unavailable", 2,
        "تمام احجزيه", conversation_id,
    )
    turns.append(booking_turn)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before_booking, after)
    competing_after = db.get(Appointment, competing.id)
    ok = not created and competing_after is not None and competing_after.status == "confirmed"
    return make_result(
        scenario_id="b5_04_slot_becomes_unavailable",
        category="changing_scheduling_truth",
        purpose="A slot verified earlier must not remain writable after canonical availability changes.",
        turns=turns, before=before_booking, after=after,
        verification={
            "requested_start_at": slot.start_at.isoformat(),
            "competing_appointment_id": str(competing.id),
            "created_for_customer": created,
            "booking_turn_reads": booking_turn.verified_reads,
            "booking_write_result": booking_turn.write_result,
        },
        deterministic_ok=ok,
        expected="No double booking; write-time/canonical validation wins and the stale slot is reported unavailable.",
        issue_severity="P1",
        issue_title="Stale availability was treated as permission to double-book",
    )


def case_05_doctor_schedule_changes_between_turns(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    doctor_row, doctor_model, available, slot = _regular_doctor_slot(db, workspace, service)
    day, time_text = local_slot(available, slot)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b5_05_doctor_schedule_changes_between_turns",
        [f"هل {service.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor_row)} متاح؟"],
    )
    local_day = slot.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date()
    weekday = local_day.weekday()
    branch_id = UUID(str(slot.branch_id))
    hours_delete = db.execute(
        delete(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == workspace.id,
            DoctorWorkingHour.doctor_id == doctor_model.id,
            DoctorWorkingHour.branch_id == branch_id,
            DoctorWorkingHour.weekday == weekday,
        )
    )
    db.execute(
        delete(DoctorAvailabilityWindow).where(
            DoctorAvailabilityWindow.workspace_id == workspace.id,
            DoctorAvailabilityWindow.doctor_id == doctor_model.id,
            DoctorAvailabilityWindow.branch_id == branch_id,
            DoctorAvailabilityWindow.start_at < slot.end_at,
            DoctorAvailabilityWindow.end_at > slot.start_at,
        )
    )
    db.flush()
    before_booking = extended_state_snapshot(db, workspace, patient)
    _, booking_turn = send_turn(
        db, workspace, patient, "b5_05_doctor_schedule_changes_between_turns", 2,
        "تمام احجزيه", conversation_id,
    )
    turns.append(booking_turn)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before_booking, after)
    fresh_read = "availability" in booking_turn.verified_reads
    ok = not created and fresh_read
    return make_result(
        scenario_id="b5_05_doctor_schedule_changes_between_turns",
        category="changing_scheduling_truth",
        purpose="Doctor schedule changes after an earlier availability answer must invalidate that answer.",
        turns=turns, before=before_booking, after=after,
        verification={
            "doctor_id": str(doctor_model.id),
            "requested_start_at": slot.start_at.isoformat(),
            "working_hours_removed": int(hours_delete.rowcount or 0),
            "booking_turn_reads": booking_turn.verified_reads,
            "created_for_customer": created,
        },
        deterministic_ok=ok,
        expected="Fresh canonical doctor availability is checked before write; no stale-slot booking occurs.",
        issue_severity="P1" if created else "P2",
        issue_title="Doctor schedule change was ignored or not revalidated",
    )
def case_06_cancelled_externally_before_reschedule(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _source_av, source_slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    source = _seed_future_appointment(
        db, workspace, patient, service=service,
        doctor_id=UUID(str(source_slot.doctor_id)), slot=source_slot,
    )
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b5_06_cancelled_externally_before_reschedule",
        ["ممكن أغير ميعادي؟"],
    )
    source.status = "cancelled"
    source.cancelled_at = datetime.now(UTC)
    db.flush()
    target_av, target_slot = _future_slot(
        db, workspace, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=source.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC")).date(),
        exclude_appointment_id=str(source.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    _, second = send_turn(
        db, workspace, patient, "b5_06_cancelled_externally_before_reschedule", 2,
        f"خليه يوم {target_date} الساعة {target_time}", conversation_id,
    )
    turns.append(second)
    after = extended_state_snapshot(db, workspace, patient)
    original = _appointment_by_id(after, source.id)
    replacements = _replacement_rows(after, source.id)
    created = created_appointments(before, after)
    ok = original["status"] == "cancelled" and not replacements and not created
    return make_result(
        scenario_id="b5_06_cancelled_externally_before_reschedule",
        category="changing_scheduling_truth",
        purpose="Externally cancelled appointments must not be resurrected by stale conversational reschedule state.",
        turns=turns, before=before, after=after,
        verification={
            "source_id": str(source.id),
            "external_status_before_customer_turn": "cancelled",
            "appointment_after": original,
            "replacements": replacements,
            "second_turn_reads": second.verified_reads,
        },
        deterministic_ok=ok,
        expected="Canonical cancelled state wins; no reschedule/revival write occurs.",
        issue_severity="P1",
        issue_title="Cancelled canonical appointment was resurrected from stale state",
    )


def case_07_same_day_current_time_boundary(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    timezone = ZoneInfo(workspace.timezone or "UTC")
    wall_clock_now = datetime.now(UTC).astimezone(timezone)
    booking_settings = get_effective_booking_settings(db, workspace.id)
    probe_now = wall_clock_now.replace(hour=0, minute=0, second=0, microsecond=0)
    available = adapter.get_availability(
        AvailabilityRequest(
            branch_id=branch_id,
            service_id=str(service.id),
            booking_date=wall_clock_now.date(),
            now=probe_now.astimezone(UTC),
        )
    )
    if not available.slots:
        raise RuntimeError("EVAL_INFRA_ERROR: no same-day canonical slot for boundary scenario")
    slot = available.slots[0]
    scenario_now_utc = slot.start_at - timedelta(
        minutes=booking_settings.minimum_notice_minutes
    )
    local_now = scenario_now_utc.astimezone(timezone)
    if local_now.date() != wall_clock_now.date():
        raise RuntimeError("EVAL_INFRA_ERROR: same-day boundary clock escaped current local date")
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    class _ScenarioDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return scenario_now_utc.replace(tzinfo=None)
            return scenario_now_utc.astimezone(tz)

    original_clock = live_chat_module._workspace_clock
    original_booking_datetime = booking_module.datetime
    live_chat_module._workspace_clock = lambda _workspace: (timezone.key, local_now)
    booking_module.datetime = _ScenarioDateTime
    try:
        turns, _ = _run_messages(
            db, workspace, patient, "b5_07_same_day_current_time_boundary",
            [f"احجزيلي {service.name} النهارده {day} الساعة {time_text} مع {doctor_name(doctor)}"],
        )
    finally:
        booking_module.datetime = original_booking_datetime
        live_chat_module._workspace_clock = original_clock
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    exact = (
        len(created) == 1
        and created[0]["start_at"] == slot.start_at.isoformat()
        and created[0]["doctor_id"] == str(slot.doctor_id)
    )
    notice_minutes = int((slot.start_at - scenario_now_utc).total_seconds() // 60)
    wrong_write = bool(created) and not exact
    ok = exact
    return make_result(
        scenario_id="b5_07_same_day_current_time_boundary",
        category="changing_scheduling_truth",
        purpose="Same-day booking must follow the current canonical notice rule, not a stale one-hour assumption.",
        turns=turns, before=before, after=after,
        verification={
            "local_now": local_now.isoformat(),
            "canonical_minimum_notice_minutes": booking_settings.minimum_notice_minutes,
            "requested_notice_minutes": notice_minutes,
            "requested_start_at": slot.start_at.isoformat(),
            "created": created,
        },
        deterministic_ok=ok,
        expected="The earliest canonical same-day slot is bookable exactly when current business rules allow it.",
        issue_severity="P1" if wrong_write else "P2",
        issue_title="Same-day boundary did not match the current canonical booking rule",
    )


def case_08_doctor_does_not_offer_service(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    catalog = build_clinic_catalog(db, workspace)
    selected: tuple[Service, dict[str, Any], dict[str, Any]] | None = None
    for service_row in catalog.get("services", []):
        if not isinstance(service_row, dict) or not service_row.get("id"):
            continue
        if service_row.get("requires_laser_device"):
            continue
        valid_doctors = [str(value) for value in (service_row.get("doctor_ids") or [])]
        if not valid_doctors:
            continue
        incompatible = next(
            (
                doctor for doctor in catalog.get("doctors", [])
                if isinstance(doctor, dict)
                and doctor.get("id")
                and str(doctor["id"]) not in valid_doctors
            ),
            None,
        )
        compatible = next(
            (
                doctor for doctor in catalog.get("doctors", [])
                if isinstance(doctor, dict) and str(doctor.get("id")) in valid_doctors
            ),
            None,
        )
        if incompatible is None or compatible is None:
            continue
        service = db.get(Service, UUID(str(service_row["id"])))
        if service is None or not service.is_active:
            continue
        try:
            available, slot = _availability_for(
                db, workspace, service=service,
                doctor_id=UUID(str(compatible["id"])),
                after_date=datetime.now(UTC).date() + timedelta(days=1),
            )
        except RuntimeError:
            continue
        selected = (service, incompatible, {"available": available, "slot": slot})
        break
    if selected is None:
        raise RuntimeError("EVAL_INFRA_ERROR: no incompatible doctor/service pair")
    service, incompatible_doctor, slot_context = selected
    available = slot_context["available"]
    slot = slot_context["slot"]
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b5_08_doctor_does_not_offer_service",
        [f"احجزيلي {service.name} يوم {day} الساعة {time_text} مع {doctor_name(incompatible_doctor)}"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = not created
    return make_result(
        scenario_id="b5_08_doctor_does_not_offer_service",
        category="compatibility",
        purpose="An explicit doctor who does not offer the requested service must never be silently substituted into a booking.",
        turns=turns, before=before, after=after,
        verification={
            "service_id": str(service.id),
            "incompatible_doctor_id": str(incompatible_doctor["id"]),
            "requested_date": day,
            "requested_time": time_text,
            "created": created,
            "verified_reads": turns[-1].verified_reads,
        },
        deterministic_ok=ok,
        expected="No incompatible booking; give a grounded correction or valid alternatives.",
        issue_severity="P1",
        issue_title="Explicit incompatible doctor/service pair produced a booking",
    )


def case_09_laser_requires_device(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _catalog, service_row, doctor, available, slot = laser_context(
        db, workspace, service_slug="laser-hair-removal-underarm", device_key="prime_lase",
    )
    service = db.get(Service, UUID(str(service_row["id"])))
    if service is None:
        raise RuntimeError("EVAL_INFRA_ERROR: laser service missing")
    valid_devices = _valid_device_keys(db, workspace, service)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b5_09_laser_requires_device",
        [f"احجزيلي {service.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor)}"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    invented_device = any(
        row.get("laser_device_key") not in valid_devices
        for row in created
    )
    safe_resolution = len(created) <= 1 and not invented_device
    if created:
        safe_resolution = safe_resolution and "availability" in turns[-1].verified_reads
    return make_result(
        scenario_id="b5_09_laser_requires_device",
        category="compatibility",
        purpose="A device-required laser service must resolve a compatible device canonically or clarify.",
        turns=turns, before=before, after=after,
        verification={
            "service_id": str(service.id),
            "valid_device_keys": sorted(valid_devices),
            "created": created,
            "verified_reads": turns[-1].verified_reads,
        },
        deterministic_ok=safe_resolution,
        expected="No invented device; any booking uses a verified compatible device, otherwise the Agent clarifies.",
        issue_severity="P1",
        issue_title="Laser booking used an invented or unverified device",
    )


def case_10_explicit_incompatible_device(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _catalog, service_row, doctor, available, slot = laser_context(
        db, workspace, service_slug="laser-hair-removal-underarm", device_key="prime_lase",
    )
    service = db.get(Service, UUID(str(service_row["id"])))
    if service is None:
        raise RuntimeError("EVAL_INFRA_ERROR: laser service missing")
    incompatible_price = db.scalar(
        select(ServiceDevicePrice).where(
            ServiceDevicePrice.workspace_id == workspace.id,
            ServiceDevicePrice.service_id == service.id,
            ServiceDevicePrice.device_key == "candela_gentle",
        )
    )
    if incompatible_price is None:
        raise RuntimeError("EVAL_INFRA_ERROR: Candela fixture missing")
    incompatible_price.is_active = False
    db.flush()
    valid_devices = _valid_device_keys(db, workspace, service)
    day, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db, workspace, patient, "b5_10_explicit_incompatible_device",
        [
            (
                f"احجزيلي {service.name} يوم {day} الساعة {time_text} "
                f"مع {doctor_name(doctor)} على Candela Gentle"
            )
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = not created
    return make_result(
        scenario_id="b5_10_explicit_incompatible_device",
        category="compatibility",
        purpose="An explicitly requested device that is not canonical for the service must not be silently retained or substituted.",
        turns=turns, before=before, after=after,
        verification={
            "service_id": str(service.id),
            "explicit_device": "candela_gentle",
            "valid_device_keys_after_fixture_change": sorted(valid_devices),
            "created": created,
            "verified_reads": turns[-1].verified_reads,
        },
        deterministic_ok=ok,
        expected="No booking with the incompatible explicit service/device pair; correction or clarification only.",
        issue_severity="P1",
        issue_title="Explicit incompatible device survived canonical compatibility validation",
    )
def case_11_doctor_change_invalidates_availability(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    catalog = build_clinic_catalog(db, workspace)
    doctors = [
        row for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and row.get("id")
        and str(service.id) in {str(value) for value in (row.get("service_ids") or [])}
    ]
    if len(doctors) < 2:
        raise RuntimeError("EVAL_INFRA_ERROR: fewer than two Hydrafacial doctors")
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    branch_id = active_branch_id(catalog)
    context = None
    for offset in range(2, 40):
        day = datetime.now(UTC).date() + timedelta(days=offset)
        for doctor_a in doctors:
            av_a = adapter.get_availability(AvailabilityRequest(
                branch_id=branch_id,
                service_id=str(service.id),
                booking_date=day,
                doctor_id=str(doctor_a["id"]),
            ))
            if not av_a.slots:
                continue
            for doctor_b in doctors:
                if str(doctor_b["id"]) == str(doctor_a["id"]):
                    continue
                av_b = adapter.get_availability(AvailabilityRequest(
                    branch_id=branch_id,
                    service_id=str(service.id),
                    booking_date=day,
                    doctor_id=str(doctor_b["id"]),
                ))
                if not av_b.slots:
                    continue
                starts_b = {slot.start_at for slot in av_b.slots}
                unique = next((slot for slot in av_a.slots if slot.start_at not in starts_b), None)
                if unique is not None:
                    context = (doctor_a, doctor_b, av_a, av_b, unique)
                    break
            if context is not None:
                break
        if context is not None:
            break
    if context is None:
        raise RuntimeError("EVAL_INFRA_ERROR: no doctor-switch availability contrast")
    doctor_a, doctor_b, av_a, av_b, unique_slot = context
    day, _ = local_slot(av_a, unique_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b5_11_doctor_change_invalidates_availability",
        [f"عايزة أحجز {service.name} يوم {day} مع {doctor_name(doctor_a)}، المواعيد إيه؟"],
    )
    _, changed = send_turn(
        db, workspace, patient, "b5_11_doctor_change_invalidates_availability", 2,
        f"لا خليها مع {doctor_name(doctor_b)}", conversation_id,
    )
    turns.append(changed)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    wrong_doctor = any(row["doctor_id"] != str(doctor_b["id"]) for row in created)
    fresh_read = "availability" in changed.verified_reads
    ok = fresh_read and not wrong_doctor
    return make_result(
        scenario_id="b5_11_doctor_change_invalidates_availability",
        category="compatibility",
        purpose="Changing doctors must invalidate availability shown for the previous doctor.",
        turns=turns, before=before, after=after,
        verification={
            "doctor_a_id": str(doctor_a["id"]),
            "doctor_b_id": str(doctor_b["id"]),
            "date": day,
            "doctor_a_unique_start": unique_slot.start_at.isoformat(),
            "doctor_b_slot_count": len(av_b.slots),
            "changed_turn_reads": changed.verified_reads,
            "created": created,
        },
        deterministic_ok=ok,
        expected="Doctor B availability is freshly verified; no Doctor A slot is reused.",
        issue_severity="P1" if wrong_doctor else "P2",
        issue_title="Doctor change reused stale availability from the previous doctor",
    )
def case_12_duplicate_booking_after_external_change(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    available, slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    catalog = build_clinic_catalog(db, workspace)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, time_text = local_slot(available, slot)
    initial_before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b5_12_duplicate_booking_after_external_change",
        [f"احجزيلي {service.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor)}"],
    )
    initial_after = extended_state_snapshot(db, workspace, patient)
    created_initial = created_appointments(initial_before, initial_after)
    if len(created_initial) != 1:
        return make_result(
            scenario_id="b5_12_duplicate_booking_after_external_change",
            category="stale_duplicate_safety",
            purpose="Recent-action acknowledgement must not hide later canonical appointment changes.",
            turns=turns, before=initial_before, after=initial_after,
            verification={"initial_created": created_initial},
            deterministic_ok=False,
            expected="Initial booking must succeed before the external state-change fixture.",
            issue_severity="P2",
            issue_title="Initial booking prerequisite failed",
        )
    appointment = db.get(Appointment, UUID(created_initial[0]["id"]))
    if appointment is None:
        raise RuntimeError("EVAL_INFRA_ERROR: created appointment missing")
    appointment.status = "cancelled"
    appointment.cancelled_at = datetime.now(UTC)
    db.flush()
    before_repeat = extended_state_snapshot(db, workspace, patient)
    _, repeat = send_turn(
        db, workspace, patient, "b5_12_duplicate_booking_after_external_change", 2,
        "اه تمام احجزيه كده", conversation_id,
    )
    turns.append(repeat)
    after = extended_state_snapshot(db, workspace, patient)
    active = [
        row for row in after["appointments"]
        if row["status"] in {"pending", "confirmed"}
    ]
    canonical_recheck = bool({"appointments", "availability"} & set(repeat.verified_reads))
    no_duplicate = len(active) <= 1
    ok = canonical_recheck and no_duplicate
    return make_result(
        scenario_id="b5_12_duplicate_booking_after_external_change",
        category="stale_duplicate_safety",
        purpose="A repeated confirmation after an external state change must consult canonical state instead of blindly acknowledging the old action.",
        turns=turns, before=before_repeat, after=after,
        verification={
            "externally_cancelled_appointment_id": str(appointment.id),
            "repeat_verified_reads": repeat.verified_reads,
            "repeat_write_attempted": repeat.write_attempted,
            "active_appointments_after": active,
        },
        deterministic_ok=ok,
        expected="Recent-action context never masks the external canonical change; at most one active booking remains.",
        issue_severity="P1" if not no_duplicate else "P2",
        issue_title="Recent booking acknowledgement ignored changed canonical appointment state",
    )


def case_13_repeat_cancel_after_external_change(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    available, slot = _availability_for(
        db, workspace, service=service,
        after_date=datetime.now(UTC).date() + timedelta(days=2),
    )
    appointment = _seed_future_appointment(
        db, workspace, patient, service=service,
        doctor_id=UUID(str(slot.doctor_id)), slot=slot,
    )
    day, time_text = local_slot(available, slot)
    conversation = _seed_history_conversation(
        db,
        workspace,
        patient,
        [(f"الغِي ميعاد {service.name} يوم {day}", f"تم إلغاء ميعاد {service.name} يوم {day} الساعة {time_text}.")],
    )
    appointment.status = "cancelled"
    appointment.cancelled_at = datetime.now(UTC)
    db.flush()
    before = extended_state_snapshot(db, workspace, patient)
    _, turn = send_turn(
        db, workspace, patient, "b5_13_repeat_cancel_after_external_change", 1,
        "الغيه", conversation.id,
    )
    after = extended_state_snapshot(db, workspace, patient)
    row = _appointment_by_id(after, appointment.id)
    delta = db_delta(before, after)
    ok = (
        row["status"] == "cancelled"
        and _business_delta_empty(delta)
        and turn.write_result != "completed"
    )
    return make_result(
        scenario_id="b5_13_repeat_cancel_after_external_change",
        category="stale_duplicate_safety",
        purpose="Repeating cancellation after canonical state already changed must be non-destructive.",
        turns=[turn], before=before, after=after,
        verification={
            "appointment_id": str(appointment.id),
            "appointment_after": row,
            "verified_reads": turn.verified_reads,
            "write_attempted": turn.write_attempted,
            "write_result": turn.write_result,
        },
        deterministic_ok=ok,
        expected="No duplicate destructive cancellation; canonical cancelled state is respected.",
        issue_severity="P1",
        issue_title="Repeat cancellation attempted a destructive duplicate action",
    )


def case_14_availability_then_competing_laser_booking(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    catalog, service_row, doctor, available, slot = laser_context(
        db, workspace, service_slug="laser-hair-removal-underarm", device_key="candela_gentle",
    )
    service = db.get(Service, UUID(str(service_row["id"])))
    if service is None:
        raise RuntimeError("EVAL_INFRA_ERROR: laser service missing")
    day, time_text = local_slot(available, slot)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b5_14_availability_then_competing_laser_booking",
        [
            (
                f"هل {service.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor)} "
                "على Candela Gentle متاح؟"
            )
        ],
    )
    competitor = _new_patient(db, workspace, first_name="عميل", last_name="سباق")
    other_doctor = next(
        (
            row for row in catalog.get("doctors", [])
            if isinstance(row, dict)
            and row.get("id")
            and str(row["id"]) != str(doctor["id"])
            and str(service.id) in {str(value) for value in (row.get("service_ids") or [])}
        ),
        doctor,
    )
    competing = _seed_future_appointment(
        db, workspace, competitor, service=service,
        doctor_id=UUID(str(other_doctor["id"])), slot=slot,
        device_key="candela_gentle",
    )
    before_booking = extended_state_snapshot(db, workspace, patient)
    _, confirm = send_turn(
        db, workspace, patient, "b5_14_availability_then_competing_laser_booking", 2,
        "تمام احجزيه", conversation_id,
    )
    turns.append(confirm)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before_booking, after)
    competitor_after = db.get(Appointment, competing.id)
    ok = (
        not created
        and competitor_after is not None
        and competitor_after.status == "confirmed"
    )
    return make_result(
        scenario_id="b5_14_availability_then_competing_laser_booking",
        category="stale_duplicate_safety",
        purpose="A competing device booking after availability read must defeat the stale write safely.",
        turns=turns, before=before_booking, after=after,
        verification={
            "device_key": "candela_gentle",
            "requested_start_at": slot.start_at.isoformat(),
            "competing_appointment_id": str(competing.id),
            "confirm_reads": confirm.verified_reads,
            "confirm_write_attempted": confirm.write_attempted,
            "confirm_write_result": confirm.write_result,
            "created_for_customer": created,
        },
        deterministic_ok=ok,
        expected="No device double-booking and no false successful write after the competing booking.",
        issue_severity="P1",
        issue_title="Competing booking did not invalidate stale laser availability",
    )
def case_15_two_rapid_customer_turns(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    catalog = build_clinic_catalog(db, workspace)
    context = None
    for doctor in catalog.get("doctors", []):
        if not isinstance(doctor, dict) or not doctor.get("id"):
            continue
        if str(service.id) not in {str(value) for value in (doctor.get("service_ids") or [])}:
            continue
        try:
            available, _ = _availability_for(
                db, workspace, service=service,
                doctor_id=UUID(str(doctor["id"])),
                after_date=datetime.now(UTC).date() + timedelta(days=1),
            )
        except RuntimeError:
            continue
        same_day = [slot for slot in available.slots if slot.start_at.date() == available.slots[0].start_at.date()]
        if len(same_day) >= 2:
            context = (doctor, available, same_day[0], same_day[-1])
            break
    if context is None:
        raise RuntimeError("EVAL_INFRA_ERROR: no two-slot rapid-turn context")
    doctor, available, first_slot, second_slot = context
    first_day, first_time = local_slot(available, first_slot)
    _second_day, second_time = local_slot(available, second_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db, workspace, patient, "b5_15_two_rapid_customer_turns",
        [f"احجزيلي {service.name} يوم {first_day} الساعة {first_time} مع {doctor_name(doctor)}"],
    )
    _, correction = send_turn(
        db, workspace, patient, "b5_15_two_rapid_customer_turns", 2,
        f"لا خليها الساعة {second_time}", conversation_id,
    )
    turns.append(correction)
    after = extended_state_snapshot(db, workspace, patient)
    active = [
        row for row in after["appointments"]
        if row["status"] in {"pending", "confirmed"}
    ]
    latest_only = (
        len(active) == 1
        and active[0]["start_at"] == second_slot.start_at.isoformat()
        and active[0]["doctor_id"] == str(doctor["id"])
        and active[0]["service_id"] == str(service.id)
    )
    return make_result(
        scenario_id="b5_15_two_rapid_customer_turns",
        category="stale_duplicate_safety",
        purpose="Back-to-back booking then correction must converge to the latest legitimate intent without duplicate active appointments.",
        turns=turns, before=before, after=after,
        verification={
            "conversation_id": str(conversation_id),
            "first_requested_start": first_slot.start_at.isoformat(),
            "second_requested_start": second_slot.start_at.isoformat(),
            "turn_order": [turn.turn_number for turn in turns],
            "active_appointments_after": active,
            "correction_reads": correction.verified_reads,
            "correction_write_result": correction.write_result,
        },
        deterministic_ok=latest_only,
        expected="Exactly one active appointment remains at the corrected time; no out-of-order stale write survives.",
        issue_severity="P1",
        issue_title="Rapid successive turns left a duplicate or stale active booking",
    )
CASES: list[ScenarioFn] = [
    case_01_same_name_different_patients,
    case_02_phone_beats_ambiguous_name,
    case_03_historical_name_not_current_identity,
    case_04_slot_becomes_unavailable,
    case_05_doctor_schedule_changes_between_turns,
    case_06_cancelled_externally_before_reschedule,
    case_07_same_day_current_time_boundary,
    case_08_doctor_does_not_offer_service,
    case_09_laser_requires_device,
    case_10_explicit_incompatible_device,
    case_11_doctor_change_invalidates_availability,
    case_12_duplicate_booking_after_external_change,
    case_13_repeat_cancel_after_external_change,
    case_14_availability_then_competing_laser_booking,
    case_15_two_rapid_customer_turns,
]


def _write_reports(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Linka Agent Evaluation — Batch 05 Raw Baseline",
        "",
        f"- Batch runtime base SHA: {payload['run_metadata']['batch5_base_sha']}",
        f"- Harness SHA: {payload['run_metadata']['git_sha']}",
        f"- Scenario version: {payload['run_metadata']['scenario_version']}",
        f"- Model: {payload['run_metadata']['model']}",
        f"- Reasoning: {payload['run_metadata']['reasoning_effort']}",
        f"- Scenarios executed: {len(payload['scenario_results'])}",
        "",
        "Raw deterministic findings are guards; final verdict requires manual DB/write/response review.",
        "",
    ]
    for row in payload["scenario_results"]:
        lines.extend([
            f"## {row['id']}",
            "",
            f"Category: {row['category']}",
            f"Purpose: {row['purpose']}",
            f"Review status: {row['review']['status']}",
            f"Expected: {row['review']['expected']}",
            "",
        ])
        for turn in row["turns"]:
            lines.append(f"Customer {turn['turn_number']}: {turn['user_message']}")
            lines.append(f"Linka: {turn['agent_response']}")
            lines.append(
                "Usage: "
                f"in={turn['token_usage']['input_tokens']} "
                f"read={turn['token_usage']['cached_tokens']} "
                f"write={turn['token_usage']['cache_write_tokens']} "
                f"uncached={turn['token_usage']['uncached_input_tokens']} "
                f"out={turn['token_usage']['output_tokens']} "
                f"latency={turn['latency_ms']}ms"
            )
            lines.append("")
        lines.append("DB verification:")
        lines.append(json.dumps(row["db_verification"], ensure_ascii=False, indent=2))
        if row["issues"]:
            lines.append("Deterministic findings:")
            for issue in row["issues"]:
                lines.append(f"- {issue['severity']}: {issue['title']} — {issue['detail']}")
        else:
            lines.append("Deterministic findings: none; manual review still required.")
        lines.append("")
    lines.extend([
        "## Batch summary",
        "",
        json.dumps(payload["batch_summary"], ensure_ascii=False, indent=2),
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")


def _emit_compact(payload: dict[str, Any]) -> None:
    for row in payload["scenario_results"]:
        compact = {
            "id": row["id"],
            "category": row["category"],
            "execution_error": row.get("execution_error"),
            "issues": row.get("issues") or [],
            "token_usage": row.get("token_usage") or {},
            "cost": row.get("cost") or {},
            "turns": [
                {
                    "turn_number": turn["turn_number"],
                    "user_message": turn["user_message"],
                    "agent_response": turn.get("agent_response"),
                    "verified_reads": turn.get("verified_reads") or [],
                    "write_attempted": turn.get("write_attempted"),
                    "write_result": turn.get("write_result"),
                    "handoff_state": turn.get("handoff_state"),
                    "runtime_state": turn.get("runtime_state") or {},
                }
                for turn in row.get("turns") or []
            ],
            "db_verification": row.get("db_verification") or {},
        }
        print(
            "EVAL_SCENARIO_COMPACT="
            + json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
    print("EVAL_COMPACT_END", flush=True)


def main() -> int:
    ns = parse_args()
    require_explicit_demo_eval()
    if not all(value > 0 for value in (
        ns.input_price_per_million,
        ns.cached_input_price_per_million,
        ns.output_price_per_million,
    )):
        raise RuntimeError("Current provider pricing must be supplied explicitly.")

    engine = __import__("sqlalchemy").create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == ns.workspace_slug))
        if workspace is None:
            raise RuntimeError("Demo workspace not found.")
        assert_demo_only(workspace)
        _ensure_batch2_catalog_fixtures(db, workspace)
        demo_seed = {
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "history_loader_limit": settings.agent_history_messages,
            "fixture_version": BATCH5_FIXTURE_VERSION,
        }

    results: list[ScenarioResult] = []
    stopped_for_p0 = False
    for case_fn in CASES:
        row = _run_case(engine, ns.workspace_slug, case_fn)
        _apply_cost(
            row,
            input_price=ns.input_price_per_million,
            cached_price=ns.cached_input_price_per_million,
            output_price=ns.output_price_per_million,
            cache_write_multiplier=ns.cache_write_multiplier,
        )
        results.append(row)
        if any(issue.get("severity") == "P0" for issue in row.issues):
            stopped_for_p0 = True
            break

    summary = summarize(results)
    total_actual_cost = sum(float(row.cost.get("actual_total_usd") or 0) for row in results)
    total_without_cache = sum(
        float(row.cost.get("without_explicit_cache_usd") or 0) for row in results
    )
    summary["cost"] = {
        "actual_usd": round(total_actual_cost, 8),
        "without_explicit_cache_usd": round(total_without_cache, 8),
        "saving_usd": round(max(0.0, total_without_cache - total_actual_cost), 8),
        "saving_percent": round(
            ((total_without_cache - total_actual_cost) / total_without_cache * 100.0)
            if total_without_cache > 0
            else 0.0,
            2,
        ),
    }
    summary["stopped_for_deterministic_p0"] = stopped_for_p0

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(ns.output_dir)
    payload = {
        "run_metadata": {
            "batch": BATCH_NUMBER,
            "scenario_version": SCENARIO_VERSION,
            "git_sha": ns.git_sha,
            "batch5_base_sha": ns.batch5_base_sha,
            "workspace": ns.workspace_slug,
            "model": settings.openai_model,
            "fallback_model": settings.openai_fallback_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "fallback_reasoning_effort": settings.openai_fallback_reasoning_effort,
            "generated_at": datetime.now(UTC).isoformat(),
            "pricing": {
                "input_per_million": ns.input_price_per_million,
                "cached_input_per_million": ns.cached_input_price_per_million,
                "output_per_million": ns.output_price_per_million,
                "cache_write_multiplier": ns.cache_write_multiplier,
                "source": ns.pricing_source,
            },
            "demo_seed": demo_seed,
        },
        "scenario_results": [jsonable(row) for row in results],
        "batch_summary": summary,
    }
    json_path = output_dir / f"batch_05_raw_{timestamp}.json"
    md_path = output_dir / f"batch_05_raw_{timestamp}.md"
    _write_reports(payload, json_path, md_path)
    _emit_compact(payload)

    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    ).decode("ascii")
    print("EVAL_REPORT_B64_BEGIN", flush=True)
    for offset in range(0, len(encoded), 3000):
        print(f"EVAL_REPORT_B64={encoded[offset:offset + 3000]}", flush=True)
    print("EVAL_REPORT_B64_END", flush=True)
    print(f"JSON_RESULT={json_path}")
    print(f"MD_RESULT={md_path}")
    print(f"SCENARIOS_RUN={len(results)}")
    print(f"TOTAL_TURNS={summary['total_turns']}")
    print(f"TOTAL_LLM_CALLS={summary['total_llm_calls']}")
    print(f"INTERPRETER_CALLS={summary['interpreter_calls']}")
    print(f"RESPONDER_CALLS={summary['responder_calls']}")
    print(f"TOTAL_TOKENS={summary['tokens']['total_tokens']}")
    print(f"ACTUAL_COST_USD={summary['cost']['actual_usd']}")
    print(f"WITHOUT_CACHE_USD={summary['cost']['without_explicit_cache_usd']}")
    print(f"CACHE_SAVING_PERCENT={summary['cost']['saving_percent']}")
    return 2 if stopped_for_p0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
