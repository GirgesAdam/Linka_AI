from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.agents.availability_presentation import availability_windows_from_slots
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.tia_database_laser import TiaDatabaseLaserClinicAdapter
from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.doctor import Doctor
from app.models.working_hours import DoctorWorkingHour
from app.services.agent_v2.reference_resolution import build_availability_reference_options
from tools.agent_eval import tmp_s9_ra10_audit as old
from tools.agent_eval import tmp_s9_ra_base as base

OUT = Path("audit-output")
OUT.mkdir(exist_ok=True)
TARGET = date(2026, 10, 20)
CANDELA = "candela"
CAIRO = ZoneInfo("Africa/Cairo")
PROBES = ("15:45", "16:00", "16:15", "16:30", "16:45", "17:00", "17:15", "17:30")


def slot_payload(slot: Any) -> dict[str, Any]:
    local_start = slot.start_at.astimezone(CAIRO)
    local_end = slot.end_at.astimezone(CAIRO)
    return {
        "start_at": slot.start_at.isoformat(),
        "start_local": local_start.isoformat(),
        "end_at": slot.end_at.isoformat(),
        "end_local": local_end.isoformat(),
        "doctor_id": slot.doctor_id,
        "doctor_name": slot.doctor_name,
        "device_key": slot.laser_device_key,
        "device_name": slot.laser_device_name,
        "service_id": slot.service_id,
        "service_name": slot.service_name,
        "duration_minutes": slot.duration_minutes,
        "price_minor": slot.price_minor,
        "currency": slot.currency,
    }


def probe(flow: base.Flow, raw: list[dict[str, Any]], *, doctor_id: str, service: Any, hhmm: str) -> dict[str, Any]:
    start_local = datetime.fromisoformat(f"2026-10-20T{hhmm}:00+03:00")
    duration = next((int(row["duration_minutes"]) for row in raw if row["device_key"] == CANDELA), int(service.duration_minutes))
    end_local = start_local + timedelta(minutes=duration)
    exact = next((row for row in raw if row["start_local"][11:16] == hhmm), None)
    if exact is not None:
        return {"time": hhmm, "available": True, "reason": None, "slot": exact}
    doctor_uuid = next(row.id for row in flow.db.scalars(select(Doctor).where(Doctor.workspace_id == flow.workspace.id)) if str(row.id) == doctor_id)
    branch = old._branch(flow.db, flow.workspace)
    hours = list(flow.db.scalars(select(DoctorWorkingHour).where(DoctorWorkingHour.workspace_id == flow.workspace.id, DoctorWorkingHour.doctor_id == doctor_uuid, DoctorWorkingHour.branch_id == branch.id, DoctorWorkingHour.weekday == 1)))
    clock_start = start_local.timetz().replace(tzinfo=None)
    clock_end = end_local.timetz().replace(tzinfo=None)
    if not any(row.start_time <= clock_start and clock_end <= row.end_time for row in hours):
        return {"time": hhmm, "available": False, "reason": "outside_doctor_working_hours"}
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)
    blockers = list(flow.db.scalars(select(Appointment).where(Appointment.workspace_id == flow.workspace.id, Appointment.branch_id == branch.id, Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES), Appointment.busy_start_at < end_utc, Appointment.busy_end_at > start_utc, or_(Appointment.doctor_id == doctor_uuid, Appointment.laser_device_key == CANDELA))))
    if blockers:
        return {"time": hhmm, "available": False, "reason": "busy_overlap", "blockers": [{"appointment_id": str(row.id), "busy_start_at": row.busy_start_at.isoformat(), "busy_end_at": row.busy_end_at.isoformat(), "doctor_id": str(row.doctor_id) if row.doctor_id else None, "device_key": row.laser_device_key} for row in blockers]}
    return {"time": hhmm, "available": False, "reason": "not_returned_by_canonical_booking_engine"}


def main() -> int:
    with base.rollback_env("RA-A1-BASELINE", 701) as (flow, services):
        old._prepare_candela(flow, services)
        service = services[base.SLUG["underarm"]]
        doctor_id = old._doctor_id(flow.db, flow.workspace, "يوسف", "سمير")
        old._ensure_doctor_slot(flow, doctor_id=doctor_id, service_id=str(service.id))
        old._block_slot(flow, service, doctor_id=doctor_id, date_value="2026-10-20", hhmm="16:15")
        branch = old._branch(flow.db, flow.workspace)
        adapter = TiaDatabaseLaserClinicAdapter(db=flow.db, workspace=flow.workspace)
        availability = adapter.get_availability(AvailabilityRequest(branch_id=str(branch.id), service_id=str(service.id), booking_date=TARGET, doctor_id=doctor_id, now=datetime(2026, 10, 12, 12, 0, tzinfo=UTC), laser_device_key=CANDELA))
        raw = [slot_payload(slot) for slot in availability.slots]
        verified_slots = [{**row, "laser_device_key": row["device_key"], "laser_device_name": row["device_name"], "start_time_24h": row["start_local"][11:16], "end_time_24h": row["end_local"][11:16]} for row in raw]
        windows = availability_windows_from_slots(verified_slots)
        refs = build_availability_reference_options(displayed_windows=windows, verified_slots=verified_slots)
        probes = [probe(flow, raw, doctor_id=doctor_id, service=service, hhmm=value) for value in PROBES]
        first_reply = flow.send(f"وريني مواعيد {service.name} يوم 20 أكتوبر 2026 مع د. يوسف سمير")
        ctx = old._latest_availability_context(flow)
        flow.send("كانديلا")
        flow.send("السعر كام؟")
        unavailable_reply = flow.send("الساعة 16:15")
        payload = {"head_under_test": os.getenv("BASELINE_PRODUCTION_HEAD"), "raw_canonical_slots": raw, "probes": probes, "availability_windows_after_compression": windows, "reference_options_from_raw": refs, "server_owned_reference_context_after_first_reply": ctx, "first_agent_reply": first_reply, "unavailable_1615_reply": unavailable_reply, "events": flow.events, "counts_before_rollback": flow.counts()}
    payload["persisted_after_rollback"] = dict(base.rollback_env.persisted)
    (OUT / "availability-device-baseline.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"probes": payload["probes"], "windows": payload["availability_windows_after_compression"], "first_reply": payload["first_agent_reply"], "unavailable_reply": payload["unavailable_1615_reply"], "persisted": payload["persisted_after_rollback"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
