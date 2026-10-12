from __future__ import annotations

import json
import os
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.message import Message
from app.models.staff import Staff
from app.models.working_hours import DoctorWorkingHour
from tools.agent_eval import tmp_s9_ra_base as base

OUT = Path("audit-output")
OUT.mkdir(exist_ok=True)
TARGET_TUESDAY = "2026-10-20"
TARGET_WEDNESDAY = "2026-10-21"
CANDELA = "candela"


def _doctor_id(db, workspace, first: str, last: str) -> str:
    row = db.scalar(
        select(Doctor)
        .join(Staff, Staff.id == Doctor.staff_id)
        .where(
            Doctor.workspace_id == workspace.id,
            Staff.first_name == first,
            Staff.last_name == last,
            Doctor.is_active.is_(True),
        )
    )
    if row is None:
        raise RuntimeError(f"audit doctor missing: {first} {last}")
    return str(row.id)


def _branch(db, workspace, code: str = "new-cairo") -> Branch:
    row = db.scalar(
        select(Branch).where(
            Branch.workspace_id == workspace.id,
            Branch.code == code,
        )
    )
    if row is None:
        raise RuntimeError(f"audit branch missing: {code}")
    return row


def _prepare_candela(flow: base.Flow, services: dict[str, Any]) -> None:
    db = flow.db
    workspace = flow.workspace
    device = db.scalar(
        select(ClinicLaserDevice).where(
            ClinicLaserDevice.workspace_id == workspace.id,
            ClinicLaserDevice.device_key == CANDELA,
        )
    )
    if device is None:
        db.add(
            ClinicLaserDevice(
                workspace_id=workspace.id,
                device_key=CANDELA,
                name="Candela",
                is_active=True,
            )
        )
        db.flush()
    for slug in (
        base.SLUG["combo"],
        base.SLUG["underarm"],
        base.SLUG["bikini"],
        base.SLUG["full_arms"],
        base.SLUG["full_legs"],
        base.SLUG["face"],
    ):
        service = services[slug]
        service.requires_laser_device = True
        service.operational_category = "laser"
        price = db.scalar(
            select(ServiceDevicePrice).where(
                ServiceDevicePrice.workspace_id == workspace.id,
                ServiceDevicePrice.service_id == service.id,
                ServiceDevicePrice.device_key == CANDELA,
            )
        )
        if price is None:
            db.add(
                ServiceDevicePrice(
                    workspace_id=workspace.id,
                    service_id=service.id,
                    device_key=CANDELA,
                    device_name="Candela",
                    price_minor=service.price_minor,
                    duration_minutes=service.duration_minutes,
                    currency=service.currency,
                    is_active=True,
                )
            )
        else:
            price.device_name = "Candela"
            price.price_minor = service.price_minor
            price.duration_minutes = service.duration_minutes
            price.currency = service.currency
            price.is_active = True
    db.flush()
    base.clear_catalog()


def _ensure_doctor_slot(flow: base.Flow, *, doctor_id: str, service_id: str, weekday: int = 1) -> None:
    db = flow.db
    workspace = flow.workspace
    branch = _branch(db, workspace)
    db_doctor_id = next(row.id for row in db.scalars(select(Doctor).where(Doctor.workspace_id == workspace.id)) if str(row.id) == doctor_id)
    service_uuid = next(row.id for row in db.scalars(select(base.Service).where(base.Service.workspace_id == workspace.id)) if str(row.id) == service_id)
    db_branch = db.scalar(
        select(DoctorBranch).where(
            DoctorBranch.workspace_id == workspace.id,
            DoctorBranch.doctor_id == db_doctor_id,
            DoctorBranch.branch_id == branch.id,
        )
    )
    if db_branch is None:
        db.add(
            DoctorBranch(
                workspace_id=workspace.id,
                doctor_id=db_doctor_id,
                branch_id=branch.id,
                is_primary=False,
                is_active=True,
            )
        )
    else:
        db_branch.is_active = True
    db_service = db.scalar(
        select(DoctorService).where(
            DoctorService.workspace_id == workspace.id,
            DoctorService.doctor_id == db_doctor_id,
            DoctorService.service_id == service_uuid,
        )
    )
    if db_service is None:
        db.add(
            DoctorService(
                workspace_id=workspace.id,
                doctor_id=db_doctor_id,
                service_id=service_uuid,
                is_active=True,
            )
        )
    else:
        db_service.is_active = True
    covers = list(
        db.scalars(
            select(DoctorWorkingHour).where(
                DoctorWorkingHour.workspace_id == workspace.id,
                DoctorWorkingHour.doctor_id == db_doctor_id,
                DoctorWorkingHour.branch_id == branch.id,
                DoctorWorkingHour.weekday == weekday,
            )
        )
    )
    if not any(row.start_time <= time(16, 0) < row.end_time for row in covers):
        db.add(
            DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=db_doctor_id,
                branch_id=branch.id,
                weekday=weekday,
                start_time=time(15, 0),
                end_time=time(18, 0),
            )
        )
    db.flush()
    base.clear_catalog()


def _limit_service_doctors(
    flow: base.Flow,
    *,
    service_id: str,
    doctor_ids: set[str],
) -> None:
    db = flow.db
    workspace = flow.workspace
    service_uuid = next(
        row.id
        for row in db.scalars(select(base.Service).where(base.Service.workspace_id == workspace.id))
        if str(row.id) == service_id
    )
    doctors = {
        str(row.id): row
        for row in db.scalars(select(Doctor).where(Doctor.workspace_id == workspace.id))
    }
    rows = list(
        db.scalars(
            select(DoctorService).where(
                DoctorService.workspace_id == workspace.id,
                DoctorService.service_id == service_uuid,
            )
        )
    )
    existing = {str(row.doctor_id): row for row in rows}
    for doctor_id, doctor in doctors.items():
        row = existing.get(doctor_id)
        if doctor_id in doctor_ids:
            if row is None:
                db.add(
                    DoctorService(
                        workspace_id=workspace.id,
                        doctor_id=doctor.id,
                        service_id=service_uuid,
                        is_active=True,
                    )
                )
            else:
                row.is_active = True
        elif row is not None:
            row.is_active = False
    db.flush()
    base.clear_catalog()

def _set_doctor_hours(
    flow: base.Flow,
    *,
    doctor_id: str,
    weekday: int,
    windows: tuple[tuple[time, time], ...],
) -> None:
    db = flow.db
    workspace = flow.workspace
    branch = _branch(db, workspace)
    doctor_uuid = next(
        row.id
        for row in db.scalars(select(Doctor).where(Doctor.workspace_id == workspace.id))
        if str(row.id) == doctor_id
    )
    db.execute(
        delete(DoctorWorkingHour).where(
            DoctorWorkingHour.workspace_id == workspace.id,
            DoctorWorkingHour.doctor_id == doctor_uuid,
            DoctorWorkingHour.branch_id == branch.id,
            DoctorWorkingHour.weekday == weekday,
        )
    )
    for start_time, end_time in windows:
        db.add(
            DoctorWorkingHour(
                workspace_id=workspace.id,
                doctor_id=doctor_uuid,
                branch_id=branch.id,
                weekday=weekday,
                start_time=start_time,
                end_time=end_time,
            )
        )
    db.flush()
    base.clear_catalog()

def _block_slot(flow: base.Flow, service: Any, *, doctor_id: str, date_value: str, hhmm: str) -> None:
    db = flow.db
    workspace = flow.workspace
    branch = _branch(db, workspace)
    doctor = next(row for row in db.scalars(select(Doctor).where(Doctor.workspace_id == workspace.id)) if str(row.id) == doctor_id)
    blocker = base.create_patient(db, workspace, "Block", 9900 + flow.turn_no)
    local_start = datetime.fromisoformat(f"{date_value}T{hhmm}:00+03:00")
    start_at = local_start.astimezone(UTC)
    end_at = start_at + timedelta(minutes=service.duration_minutes)
    db.add(
        Appointment(
            workspace_id=workspace.id,
            patient_id=blocker.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            doctor_assignment_known=True,
            service_id=service.id,
            status="confirmed",
            source="ai",
            start_at=start_at,
            end_at=end_at,
            busy_start_at=start_at,
            busy_end_at=end_at,
            duration_minutes=service.duration_minutes,
            price_minor=service.price_minor,
            discount_minor=0,
            currency=service.currency,
            payment_status="unpaid",
            payment_method="unknown",
            billing_context="standard",
            laser_device_key=CANDELA,
            laser_device_name="Candela",
        )
    )
    db.flush()
    base.clear_catalog()

def _task_options(task: dict[str, Any] | None) -> list[dict[str, Any]]:
    snapshot = (task or {}).get("option_snapshot") if isinstance(task, dict) else None
    options = (snapshot or {}).get("options") if isinstance(snapshot, dict) else None
    return [row for row in (options or []) if isinstance(row, dict)]


def _proposal_time(proposal: dict[str, Any] | None) -> str | None:
    if not proposal or not proposal.get("start_at"):
        return None
    return datetime.fromisoformat(str(proposal["start_at"])).astimezone(base.CAIRO).strftime("%H:%M")


def _proposal_date(proposal: dict[str, Any] | None) -> str | None:
    if not proposal or not proposal.get("start_at"):
        return None
    return datetime.fromisoformat(str(proposal["start_at"])).astimezone(base.CAIRO).date().isoformat()


def _proposal_device_ok(proposal: dict[str, Any] | None, *, additional_count: int = 1) -> bool:
    if proposal is None or proposal.get("device_key") != CANDELA:
        return False
    additional = proposal.get("additional_service_device_keys") or {}
    return len(additional) == additional_count and all(value == CANDELA for value in additional.values())


def _booking_device_ok(row: dict[str, Any] | None, *, additional_count: int = 1) -> bool:
    if row is None or row.get("laser_device_key") != CANDELA:
        return False
    lines = row.get("additional_services") or []
    return len(lines) == additional_count and all(line.get("laser_device_key") == CANDELA for line in lines)


def _latest_availability_context(flow: base.Flow) -> dict[str, Any] | None:
    if flow.conversation_id is None:
        return None
    row = flow.db.scalar(
        select(Message)
        .where(
            Message.conversation_id == flow.conversation_id,
            Message.direction == "outbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    metadata = dict(row.metadata_json or {}) if row is not None else {}
    value = metadata.get("v2_availability_reference_context")
    return value if isinstance(value, dict) else None


def _latest_reference_debug(flow: base.Flow) -> dict[str, Any]:
    if flow.conversation_id is None:
        return {"has_context": False}
    row = flow.db.scalar(
        select(Message)
        .where(
            Message.conversation_id == flow.conversation_id,
            Message.direction == "outbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    metadata = dict(row.metadata_json or {}) if row is not None else {}
    context = metadata.get("v2_availability_reference_context")
    return {
        "model": metadata.get("model"),
        "has_context": isinstance(context, dict),
        "option_refs": sorted(_context_refs(context)) if isinstance(context, dict) else [],
        "reference_semantic": metadata.get("v2_reference_semantic"),
        "read_operation": (
            metadata.get("v2_read_context", {}).get("operation_type")
            if isinstance(metadata.get("v2_read_context"), dict)
            else None
        ),
    }

def _context_dates(context: dict[str, Any] | None) -> set[str]:
    dates: set[str] = set()
    for option in (context or {}).get("availability_reference_options") or []:
        if not isinstance(option, dict):
            continue
        raw = option.get("start_local")
        if not raw and isinstance(option.get("slot"), dict):
            raw = option["slot"].get("start_local") or option["slot"].get("start_at")
        if raw:
            try:
                dates.add(datetime.fromisoformat(str(raw)).astimezone(base.CAIRO).date().isoformat())
            except ValueError:
                pass
    return dates


def _context_refs(context: dict[str, Any] | None) -> set[str]:
    return {
        str(row.get("option_ref"))
        for row in (context or {}).get("availability_reference_options") or []
        if isinstance(row, dict) and row.get("option_ref")
    }


def _context_option_identities(context: dict[str, Any] | None) -> set[tuple[str, ...]]:
    identities: set[tuple[str, ...]] = set()
    for row in (context or {}).get("availability_reference_options") or []:
        if not isinstance(row, dict):
            continue
        slot = row.get("slot") if isinstance(row.get("slot"), dict) else {}
        identities.add(
            (
                str(slot.get("start_at") or row.get("start_local") or ""),
                str(slot.get("doctor_id") or row.get("doctor_name") or ""),
                str(slot.get("laser_device_key") or slot.get("device_key") or row.get("laser_device_name") or ""),
                str(row.get("end_local") or ""),
            )
        )
    return identities


def _selected_ref(task: dict[str, Any] | None) -> str | None:
    derived = (task or {}).get("derived") if isinstance(task, dict) else None
    value = (derived or {}).get("selected_slot_ref") if isinstance(derived, dict) else None
    return str(value) if value else None


def _result(checks: list[tuple[bool, str]], flow: base.Flow, *, p1_if_write_mismatch: bool = True, **extra: Any) -> dict[str, Any]:
    result = base.verdict(checks, flow, p1_if_write_mismatch=p1_if_write_mismatch)
    result.update(extra)
    return result


def ra1() -> dict[str, Any]:
    with base.rollback_env("RA1", 201) as (f, s):
        _prepare_candela(f, s)
        yid = _doctor_id(f.db, f.workspace, "┘è┘ê╪│┘ü", "╪│┘à┘è╪▒")
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å")
        f.send("┘â╪º┘å╪»┘è┘ä╪º")
        f.send("╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è")
        f.send("╪º┘ä╪»┘â╪¬┘ê╪▒ ┘à┘è┘å╪ƒ")
        f.send("╪╖╪¿ ╪º┘ä╪│╪╣╪▒ ┘â╪º┘à╪ƒ")
        before = f.counts()
        selection_reply = f.send("╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        p = f.proposal()
        p_ok, p_detail = base.proposal_check(p, s, [base.SLUG["combo"], base.SLUG["full_arms"]], base.SLUG["combo"])
        no_early_write = f.counts() == before
        if p:
            f.button("confirm", p["proposal_id"])
        rows = base.appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        checks = [
            (p_ok, p_detail),
            (no_early_write, "selection wrote before confirmation"),
            (p is not None and str(p.get("doctor_id")) == yid, "proposal doctor is not Youssef"),
            (_proposal_time(p) == "16:00", f"proposal time={_proposal_time(p)}"),
            (_proposal_device_ok(p), "proposal lost Candela on one or more laser services"),
            (len(rows) == 1, f"appointment rows={len(rows)}"),
            (row is not None and row["doctor_id"] == yid, f"persisted doctor={row.get('doctor_id') if row else None}"),
            (row is not None and row["start_local"].split("T")[1][:5] == "16:00", f"persisted time={row.get('start_local') if row else None}"),
            (_booking_device_ok(row), "persisted booking lost Candela"),
        ]
        result = _result(checks, f, proposal=p, selection_reply=selection_reply)
        result["wrong_doctor_write"] = bool(row and row["doctor_id"] != yid)
        result["wrong_time_write"] = bool(row and row["start_local"].split("T")[1][:5] != "16:00")
        result["no_progress_loop"] = p is None
    return base.finish_rollback_result(result)


def ra2() -> dict[str, Any]:
    with base.rollback_env("RA2", 202) as (f, s):
        _prepare_candela(f, s)
        yid = _doctor_id(f.db, f.workspace, "┘è┘ê╪│┘ü", "╪│┘à┘è╪▒")
        reference_debug: list[dict[str, Any]] = []
        for message in (
            "╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å",
            "┘â╪º┘å╪»┘è┘ä╪º",
            "╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è",
            "╪º┘ä╪│╪╣╪▒ ┘â╪º┘à╪ƒ",
            "╪»┘â╪¬┘ê╪▒ ┘è┘ê╪│┘ü ╪«╪¿╪▒╪¬┘ç ╪Ñ┘è┘ç╪ƒ",
            "╪º┘ä╪╣┘è╪º╪»╪⌐ ╪¿╪¬┘é┘ü┘ä ╪º┘ä╪│╪º╪╣╪⌐ ┘â╪º┘à╪ƒ",
            "┘à╪»╪⌐ ╪º┘ä╪¼┘ä╪│╪⌐ ┘é╪» ╪Ñ┘è┘ç╪ƒ",
        ):
            f.send(message)
            reference_debug.append({"customer": message, **_latest_reference_debug(f)})
        before = f.counts()
        f.send("╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒")
        reference_debug.append(
            {"customer": "╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒", **_latest_reference_debug(f)}
        )
        p = f.proposal()
        if p:
            f.button("confirm", p["proposal_id"])
        rows = base.appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        result = _result([
            (p is not None, "proposal missing after long side-question continuation"),
            (before["appointments"] == 0, "write occurred before slot selection"),
            (p is not None and str(p.get("doctor_id")) == yid, "proposal doctor drifted"),
            (_proposal_time(p) == "16:00", "proposal time drifted"),
            (_proposal_device_ok(p), "proposal device drifted"),
            (len(rows) == 1, f"rows={len(rows)}"),
            (_booking_device_ok(row), "persisted device drifted"),
        ], f, proposal=p, reference_debug=reference_debug)
        result["wrong_doctor_write"] = bool(row and row["doctor_id"] != yid)
        result["wrong_time_write"] = bool(row and row["start_local"].split("T")[1][:5] != "16:00")
        result["no_progress_loop"] = p is None
    return base.finish_rollback_result(result)

def ra3() -> dict[str, Any]:
    with base.rollback_env("RA3", 203) as (f, s):
        _prepare_candela(f, s)
        yid = _doctor_id(f.db, f.workspace, "┘è┘ê╪│┘ü", "╪│┘à┘è╪▒")
        for message in ("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å", "┘â╪º┘å╪»┘è┘ä╪º", "╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è", "╪º┘ä╪│╪╣╪▒ ┘â╪º┘à╪ƒ"):
            f.send(message)
        f.send("╪»┘â╪¬┘ê╪▒ ┘è┘ê╪│┘ü")
        doctor_task = f.task() or {}
        doctor_bound = str((doctor_task.get("constraints") or {}).get("doctor_id") or "") == yid
        before = f.counts()
        f.send("╪º┘ä╪│╪º╪╣╪⌐ 4")
        p = f.proposal()
        result = _result([
            (doctor_bound, "doctor-only continuation did not bind Youssef"),
            (f.counts() == before, "time selection wrote before confirmation"),
            (p is not None, "time follow-up did not create proposal"),
            (p is not None and str(p.get("doctor_id")) == yid, "proposal changed doctor"),
            (_proposal_time(p) == "16:00", f"proposal time={_proposal_time(p)}"),
            (_proposal_device_ok(p), "device continuity failed"),
        ], f, proposal=p, task_after_doctor=doctor_task)
    return base.finish_rollback_result(result)


def ra4() -> dict[str, Any]:
    with base.rollback_env("RA4", 204) as (f, s):
        _prepare_candela(f, s)
        service = s[base.SLUG["underarm"]]
        yid = _doctor_id(f.db, f.workspace, "\u064a\u0648\u0633\u0641", "\u0633\u0645\u064a\u0631")
        mid = _doctor_id(f.db, f.workspace, "\u0645\u0631\u064a\u0645", "\u062d\u0633\u0646")
        for doctor_id in (yid, mid):
            _ensure_doctor_slot(f, doctor_id=doctor_id, service_id=str(service.id))
        _limit_service_doctors(f, service_id=str(service.id), doctor_ids={yid, mid})
        _set_doctor_hours(f, doctor_id=mid, weekday=1, windows=((time(16, 0), time(16, 15)),))
        _set_doctor_hours(f, doctor_id=yid, weekday=1, windows=((time(17, 0), time(17, 15)),))
        for message in (
            f"\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 {service.name}",
            "\u0643\u0627\u0646\u062f\u064a\u0644\u0627",
            "\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621 \u0627\u0644\u062c\u0627\u064a",
            "\u0627\u0644\u0633\u0639\u0631 \u0643\u0627\u0645\u061f",
        ):
            f.send(message)
        before_ctx = _latest_availability_context(f)
        displayed = (before_ctx or {}).get("availability_reference_options") or []
        before = f.counts()
        reply = f.send("\u0627\u0644\u062a\u0627\u0646\u064a")
        narrowed = f.task() or {}
        after_ctx = _latest_availability_context(f)
        remaining = (after_ctx or {}).get("availability_reference_options") or []
        constraints = narrowed.get("constraints") if isinstance(narrowed, dict) else {}
        result = _result([
            (len(displayed) == 2, f"expected two displayed doctor windows, got {len(displayed)}"),
            (f.counts() == before, "ordinal window selection wrote before a concrete time"),
            (isinstance(constraints, dict) and constraints.get("doctor_id") == yid, "second displayed window did not narrow to Youssef"),
            (narrowed.get("status") == "awaiting_choice", f"narrowed status={narrowed.get('status')}"),
            (len(remaining) == 1, f"expected one displayed doctor window after narrowing, got {len(remaining)}"),
            (f.proposal() is None, "compressed-window ordinal invented a proposal time"),
        ], f, p1_if_write_mismatch=False, reply=reply, before_context=before_ctx, after_context=after_ctx, narrowed_task=narrowed)
    return base.finish_rollback_result(result)

def ra5() -> dict[str, Any]:
    with base.rollback_env("RA5", 205) as (f, s):
        _prepare_candela(f, s)
        service = s[base.SLUG["underarm"]]
        yid = _doctor_id(f.db, f.workspace, "\u064a\u0648\u0633\u0641", "\u0633\u0645\u064a\u0631")
        mid = _doctor_id(f.db, f.workspace, "\u0645\u0631\u064a\u0645", "\u062d\u0633\u0646")
        for doctor_id in (yid, mid):
            _ensure_doctor_slot(f, doctor_id=doctor_id, service_id=str(service.id))
        _limit_service_doctors(f, service_id=str(service.id), doctor_ids={yid, mid})
        for doctor_id in (yid, mid):
            _set_doctor_hours(
                f,
                doctor_id=doctor_id,
                weekday=1,
                windows=((time(16, 0), time(16, 15)),),
            )
        for message in (
            f"\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 {service.name} \u064a\u0648\u0645 20 \u0623\u0643\u062a\u0648\u0628\u0631 2026",
            "\u0643\u0627\u0646\u062f\u064a\u0644\u0627",
            "\u0627\u0644\u0633\u0639\u0631 \u0643\u0627\u0645\u061f",
        ):
            f.send(message)
        task_before = f.task() or {}
        options_before = _task_options(task_before)
        before = f.counts()
        ambiguous_reply = f.send("\u0627\u0644\u0633\u0627\u0639\u0629 4")
        ambiguous_task = f.task() or {}
        no_guess = f.proposal() is None and f.counts() == before
        f.send("\u0645\u0639 \u062f. \u064a\u0648\u0633\u0641 \u0633\u0645\u064a\u0631")
        p = f.proposal()
        same_time_options = [
            item
            for item in options_before
            if str((item.get("payload") or {}).get("start_time_24h") or "") == "16:00"
        ]
        result = _result([
            (len(same_time_options) == 2, f"expected two 16:00 doctor options, got {len(same_time_options)} of {len(options_before)} total"),
            (no_guess, "ambiguous 16:00 guessed/wrote instead of clarifying doctor"),
            (p is not None, "doctor clarification did not create proposal"),
            (p is not None and str(p.get("doctor_id")) == yid, "clarified proposal is not Youssef"),
            (_proposal_time(p) == "16:00", "clarified proposal did not keep 16:00"),
            (_proposal_device_ok(p, additional_count=0), "clarified proposal lost Candela"),
            (f.counts() == before, "doctor clarification wrote before proposal confirmation"),
        ], f, p1_if_write_mismatch=False, ambiguous_reply=ambiguous_reply, ambiguous_task=ambiguous_task, proposal=p, options_before=options_before)
    return base.finish_rollback_result(result)

def ra6() -> dict[str, Any]:
    with base.rollback_env("RA6", 206) as (f, s):
        _prepare_candela(f, s)
        service = s[base.SLUG["underarm"]]
        yid = _doctor_id(f.db, f.workspace, "┘è┘ê╪│┘ü", "╪│┘à┘è╪▒")
        _ensure_doctor_slot(f, doctor_id=yid, service_id=str(service.id), weekday=1)
        _ensure_doctor_slot(f, doctor_id=yid, service_id=str(service.id), weekday=2)
        _limit_service_doctors(f, service_id=str(service.id), doctor_ids={yid})
        _set_doctor_hours(f, doctor_id=yid, weekday=1, windows=((time(16, 0), time(17, 0)),))
        _set_doctor_hours(f, doctor_id=yid, weekday=2, windows=((time(16, 0), time(17, 0)),))
        for message in (
            f"┘ê╪▒┘è┘å┘è ┘à┘ê╪º╪╣┘è╪» {service.name} ┘è┘ê┘à 20 ╪ú┘â╪¬┘ê╪¿╪▒ 2026",
            "┘â╪º┘å╪»┘è┘ä╪º",
            "╪º┘ä╪│╪╣╪▒ ┘â╪º┘à╪ƒ",
        ):
            f.send(message)
        before_ctx = _latest_availability_context(f)
        old_identities = _context_option_identities(before_ctx)
        f.send("╪╖╪¿ ╪º┘ä╪ú╪▒╪¿╪╣╪º╪í 21 ╪ú┘â╪¬┘ê╪¿╪▒")
        after_ctx = _latest_availability_context(f)
        new_dates = _context_dates(after_ctx)
        new_identities = _context_option_identities(after_ctx)
        leaked = bool(old_identities & new_identities)
        result = _result([
            (TARGET_TUESDAY in _context_dates(before_ctx), f"initial context dates={sorted(_context_dates(before_ctx))}"),
            (new_dates == {TARGET_WEDNESDAY}, f"scope-change context dates={sorted(new_dates)}"),
            (not leaked, "old Tuesday canonical option identity survived Wednesday scope change"),
            (f.proposal() is None, "date scope change silently produced proposal"),
            (f.counts()["appointments"] == 0, "date scope change wrote appointment"),
        ], f, p1_if_write_mismatch=False, before_context=before_ctx, after_context=after_ctx)
        result["cross_task_leakage"] = leaked
    return base.finish_rollback_result(result)

def ra7() -> dict[str, Any]:
    with base.rollback_env("RA7", 207) as (f, s):
        _prepare_candela(f, s)
        service = s[base.SLUG["underarm"]]
        yid = _doctor_id(f.db, f.workspace, "┘è┘ê╪│┘ü", "╪│┘à┘è╪▒")
        _ensure_doctor_slot(f, doctor_id=yid, service_id=str(service.id))
        _block_slot(f, service, doctor_id=yid, date_value=TARGET_TUESDAY, hhmm="16:15")
        for message in (f"┘ê╪▒┘è┘å┘è ┘à┘ê╪º╪╣┘è╪» {service.name} ┘è┘ê┘à 20 ╪ú┘â╪¬┘ê╪¿╪▒ 2026 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒", "┘â╪º┘å╪»┘è┘ä╪º", "╪º┘ä╪│╪╣╪▒ ┘â╪º┘à╪ƒ"):
            f.send(message)
        ctx = _latest_availability_context(f)
        displayed_times = {
            datetime.fromisoformat(str((row.get("slot") or {}).get("start_at") or row.get("start_local"))).astimezone(base.CAIRO).strftime("%H:%M")
            for row in (ctx or {}).get("availability_reference_options") or []
            if isinstance(row, dict) and ((isinstance(row.get("slot"), dict) and (row.get("slot") or {}).get("start_at")) or row.get("start_local"))
        }
        before = f.counts()
        reply = f.send("╪º┘ä╪│╪º╪╣╪⌐ 16:15")
        p = f.proposal()
        result = _result([
            ("16:15" not in displayed_times, f"fixture unexpectedly displayed 16:15: {sorted(displayed_times)}"),
            (f.counts() == before, "unavailable exact time wrote appointment"),
            (p is None, f"unavailable exact time silently proposed {_proposal_time(p)}"),
        ], f, p1_if_write_mismatch=False, displayed_times=sorted(displayed_times), reply=reply, proposal=p)
        result["wrong_time_write"] = f.counts() != before
        result["stale_option_write"] = f.counts() != before
    return base.finish_rollback_result(result)


def ra8() -> dict[str, Any]:
    with base.rollback_env("RA8", 208) as (f, s):
        _prepare_candela(f, s)
        yid = _doctor_id(f.db, f.workspace, "┘è┘ê╪│┘ü", "╪│┘à┘è╪▒")
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ┘è┘ê┘à 20 ╪ú┘â╪¬┘ê╪¿╪▒ 2026 ╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒ ╪╣┘ä┘ë ┘â╪º┘å╪»┘è┘ä╪º")
        p1 = f.ensure_proposal("╪¬┘à╪º┘à")
        before = f.counts()
        side_reply = f.send("╪º┘ä╪│╪╣╪▒ ╪º┘ä╪Ñ╪¼┘à╪º┘ä┘è ┘â╪º┘à╪ƒ")
        p2 = f.proposal()
        same = bool(p1 and p2 and p1.get("proposal_id") == p2.get("proposal_id") and p1.get("start_at") == p2.get("start_at"))
        no_side_write = f.counts() == before
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = base.appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        result = _result([
            (p1 is not None, "initial proposal missing"),
            (same, "informational read invalidated/changed valid proposal"),
            (no_side_write, "proposal side question wrote appointment"),
            (len(rows) == 1, f"rows={len(rows)}"),
            (row is not None and row["doctor_id"] == yid, "confirmed proposal doctor drifted"),
            (row is not None and row["start_local"].split("T")[1][:5] == "16:00", "confirmed proposal time drifted"),
            (_booking_device_ok(row), "confirmed proposal device drifted"),
        ], f, proposal_before=p1, proposal_after=p2, side_reply=side_reply)
    return base.finish_rollback_result(result)


def ra9() -> dict[str, Any]:
    with base.rollback_env("RA9", 209) as (f, s):
        _prepare_candela(f, s)
        reference_debug: list[dict[str, Any]] = []
        for message in (
            "╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å",
            "┘â╪º┘å╪»┘è┘ä╪º",
            "╪º┘ä╪½┘ä╪º╪½╪º╪í ╪º┘ä╪¼╪º┘è",
            "╪º┘ä╪│╪╣╪▒ ┘â╪º┘à╪ƒ",
            "╪º┘ä╪»┘â╪¬┘ê╪▒ ┘à┘è┘å╪ƒ",
            "┘à╪»╪⌐ ╪º┘ä╪¼┘ä╪│╪⌐ ┘é╪» ╪Ñ┘è┘ç╪ƒ",
            "╪º┘ä╪│╪º╪╣╪⌐ 16:00 ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒",
        ):
            f.send(message)
            reference_debug.append({"customer": message, **_latest_reference_debug(f)})
        p = f.proposal()
        if p:
            f.button("confirm", p["proposal_id"])
        rows = base.appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        result = _result([
            (p is not None, "proposal missing after device continuity flow"),
            (_proposal_device_ok(p), "Candela not preserved across proposal service set"),
            (len(rows) == 1, f"rows={len(rows)}"),
            (_booking_device_ok(row), "Candela not preserved on main/additional persisted rows"),
        ], f, proposal=p, reference_debug=reference_debug)
        result["device_continuity_failure"] = not (
            _proposal_device_ok(p) and _booking_device_ok(row)
        )
    return base.finish_rollback_result(result)

def ra10() -> dict[str, Any]:
    with base.rollback_env("RA10", 210) as (f, s):
        _prepare_candela(f, s)
        combo = s[base.SLUG["combo"]]
        arms = s[base.SLUG["full_arms"]]
        yid = _doctor_id(f.db, f.workspace, "┘è┘ê╪│┘ü", "╪│┘à┘è╪▒")
        for service in (combo, arms):
            _ensure_doctor_slot(f, doctor_id=yid, service_id=str(service.id))
            _limit_service_doctors(f, service_id=str(service.id), doctor_ids={yid})
        _set_doctor_hours(
            f,
            doctor_id=yid,
            weekday=1,
            windows=((time(10, 0), time(13, 0)),),
        )
        f.send("╪╣╪º┘è╪▓╪⌐ ╪ú╪¡╪¼╪▓ ╪Ñ╪¿╪╖ ┘ê╪¿┘â┘è┘å┘è ┘ê╪░╪▒╪º╪╣┘è┘å ┘â╪º┘à┘ä┘è┘å ┘è┘ê┘à 20 ╪ú┘â╪¬┘ê╪¿╪▒ 2026 ╪º┘ä╪│╪º╪╣╪⌐ 11 ╪╡╪¿╪º╪¡┘ï╪º ┘à╪╣ ╪». ┘è┘ê╪│┘ü ╪│┘à┘è╪▒ ╪╣┘ä┘ë ┘â╪º┘å╪»┘è┘ä╪º")
        p1 = f.ensure_proposal("╪¬┘à╪º┘à")
        f.send("╪º┘ä╪│╪╣╪▒ ╪º┘ä╪Ñ╪¼┘à╪º┘ä┘è ┘â╪º┘à╪ƒ")
        p_side = f.proposal()
        f.send("╪«┘ä┘è┘ç 12 ╪¿╪»┘ä 11")
        p2 = f.proposal()
        if p2 is None:
            p2 = f.ensure_proposal("╪¬┘à╪º┘à")
        before_confirm = f.counts()
        if p2:
            f.button("confirm", p2["proposal_id"])
        rows = base.appointment_rows(f.db, f.patient)
        row = rows[0] if len(rows) == 1 else None
        result = _result([
            (p1 is not None, "11:00 proposal missing"),
            (bool(p1 and p_side and p1.get("proposal_id") == p_side.get("proposal_id")), "side question invalidated 11:00 proposal"),
            (p2 is not None, "12:00 corrected proposal missing"),
            (_proposal_time(p2) == "12:00", f"corrected proposal time={_proposal_time(p2)}"),
            (p2 is not None and str(p2.get("doctor_id")) == yid, "time correction changed doctor"),
            (_proposal_device_ok(p2), "time correction changed device/service device map"),
            (before_confirm["appointments"] == 0, "time correction wrote before confirmation"),
            (len(rows) == 1, f"rows={len(rows)}"),
            (row is not None and row["start_local"].split("T")[1][:5] == "12:00", "persisted corrected time is not 12:00"),
            (_booking_device_ok(row), "persisted corrected booking lost device"),
        ], f, proposal_11=p1, proposal_after_side=p_side, proposal_12=p2)
        result["wrong_time_write"] = bool(row and row["start_local"].split("T")[1][:5] != "12:00")
        result["wrong_doctor_write"] = bool(row and row["doctor_id"] != yid)
    return base.finish_rollback_result(result)

SCENARIOS = {
    "RA1": ra1,
    "RA2": ra2,
    "RA3": ra3,
    "RA4": ra4,
    "RA5": ra5,
    "RA6": ra6,
    "RA7": ra7,
    "RA8": ra8,
    "RA9": ra9,
    "RA10": ra10,
}


def main() -> int:
    results: dict[str, Any] = {}
    requested = {item.strip() for item in os.getenv("S9_RA_SCENARIOS", "").split(",") if item.strip()}
    selected = {name: runner for name, runner in SCENARIOS.items() if not requested or name in requested}
    for name, runner in selected.items():
        try:
            results[name] = runner()
        except Exception as exc:
            results[name] = {
                "verdict": "FAIL",
                "severity": "P2",
                "failures": [f"runner exception: {type(exc).__name__}: {exc}"],
                "exception": repr(exc),
            }
        (OUT / "s9-ra10-audit.json").write_text(
            json.dumps({"head_under_test": os.getenv("GITHUB_SHA") or "local", "environment": base.settings.environment, "model": base.settings.openai_model, "runtime": "Agent V2 live", "scenarios": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    failed = [name for name, row in results.items() if row.get("verdict") != "PASS"]
    summary = {
        "failed": failed,
        "wrong_writes": sum(1 for row in results.values() if row.get("wrong_doctor_write") or row.get("wrong_time_write")),
        "partial_writes": sum(1 for row in results.values() if any("additional service" in str(item) or "service representation" in str(item) for item in row.get("failures", [])) and row.get("appointments")),
        "false_success": sum(int(row.get("false_success") or 0) for row in results.values()),
        "stale_option_writes": sum(1 for row in results.values() if row.get("stale_option_write")),
        "wrong_doctor_writes": sum(1 for row in results.values() if row.get("wrong_doctor_write")),
        "wrong_time_writes": sum(1 for row in results.values() if row.get("wrong_time_write")),
        "cross_task_leakage": sum(1 for row in results.values() if row.get("cross_task_leakage")),
        "duplicate_appointments": sum(int(row.get("duplicate_appointments") or 0) for row in results.values()),
        "persisted_audit_writes": sum(1 for row in results.values() if row.get("persisted_audit_write")),
        "no_progress_loops": sum(1 for row in results.values() if row.get("no_progress_loop")),
        "device_continuity_failures": sum(1 for row in results.values() if row.get("device_continuity_failure")),
    }
    payload = {
        "head_under_test": os.getenv("GITHUB_SHA") or "local",
        "environment": base.settings.environment,
        "model": base.settings.openai_model,
        "runtime": "Agent V2 live",
        "scenarios": results,
        "summary": summary,
    }
    (OUT / "s9-ra10-audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("S9_RA10_SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

