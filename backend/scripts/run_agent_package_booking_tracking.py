from __future__ import annotations

"""10 focused tests for Linka package booking + tracking.

Business rules under test:
- A package contains sessions for exactly one service_id.
- Booking that same service automatically reserves one package session.
- A different-service package is never used.
- Cancellation and no-show release a reserved package session.
- Reschedule transfers the same reservation; it does not reserve a second session.
- Completion consumes the reserved session.
- Refund after consumption reprices consumed sessions at standalone session price.
- A patient cannot start another active package for the same service until the
  current one is exhausted/cancelled/expired.
- Follow-up questions must never double-book or double-reserve a package session.

The runner creates all package/appointment fixtures inside per-case savepoints.
Every case is rolled back independently, then the entire run is rolled back again
unless --keep-data is explicitly supplied.
"""

import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.agent_chat import _package_refund_quote_payload
from app.services.patient_packages import (
    consume_package_usage,
    create_patient_package,
    list_patient_packages,
    release_package_usage,
    reserve_package_usage,
    transfer_package_usage,
)

from run_agent_e2e_matrix import (
    _appointments_for,
    _catalog_row,
    _find_future_slot,
    _fixture_patients,
    _format_local_slot,
    _send,
)


@dataclass
class TurnResult:
    turn: int
    message: str
    reply: str | None
    duration_ms: int
    appointment_ids_seen: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    name: str
    status: str
    duration_ms: int
    turns: list[TurnResult] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Report:
    started_at: str
    workspace_id: str
    workspace_slug: str
    rollback: bool
    results: list[CaseResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0}
        for row in self.results:
            out[row.status] = out.get(row.status, 0) + 1
        return out


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _normalized_numbers(text: str | None) -> str:
    return (text or "").translate(_ARABIC_DIGITS).replace(",", "")


def _primary_branch_row(workspace: Workspace, catalog: dict[str, Any]) -> dict[str, Any]:
    branches = list(catalog.get("branches") or [])
    if workspace.primary_branch_id is not None:
        for row in branches:
            if str(row.get("id")) == str(workspace.primary_branch_id):
                return row
        raise RuntimeError("Primary branch was not found in the active catalog.")
    if len(branches) == 1:
        return branches[0]
    raise RuntimeError("This focused suite expects Linka's current single-branch model.")


def _doctor_name(row: dict[str, Any]) -> str:
    return str(
        row.get("name")
        or row.get("display_name")
        or row.get("full_name")
        or ""
    ).strip()


def _find_doctor_and_slot(
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    *,
    branch_id: UUID,
    service_id: UUID,
):
    errors: list[str] = []
    for doctor in catalog.get("doctors") or []:
        doctor_id = doctor.get("id")
        name = _doctor_name(doctor)
        if not doctor_id or not name:
            continue
        try:
            booking_date, timezone_name, slot = _find_future_slot(
                db,
                workspace,
                branch_id=branch_id,
                service_id=service_id,
                doctor_id=UUID(str(doctor_id)),
            )
            return doctor, booking_date, timezone_name, slot
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    raise RuntimeError(
        f"No available doctor/slot for service {service_id}. Tried: {errors[:6]}"
    )


def _service(db: Session, workspace: Workspace, service_id: UUID) -> Service:
    row = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.id == service_id,
        )
    )
    if row is None:
        raise RuntimeError(f"Service not found: {service_id}")
    return row


def _appointments_for_patient(
    db: Session,
    workspace: Workspace,
    patient,
) -> list[Appointment]:
    return list(_appointments_for(db, workspace, patient))


def _send_turn(
    *,
    db: Session,
    workspace: Workspace,
    patient,
    message: str,
    conversation_id: UUID | None,
    turn_number: int,
):
    response, duration_ms = _send(
        db,
        workspace,
        patient,
        message,
        conversation_id,
    )
    rows = _appointments_for_patient(db, workspace, patient)
    return response, TurnResult(
        turn=turn_number,
        message=message,
        reply=response.reply,
        duration_ms=duration_ms,
        appointment_ids_seen=[str(row.id) for row in rows],
    )


def _create_package(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service: Service,
    name: str,
    sessions: int,
    sale_price_minor: int | None = None,
):
    sale_price = (
        int(sale_price_minor)
        if sale_price_minor is not None
        else max(
            int(service.price_minor),
            int(service.price_minor) * max(1, sessions - 1),
        )
    )
    return create_patient_package(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service.id,
        name=name,
        sessions_purchased=sessions,
        sale_price_minor=sale_price,
        amount_paid_minor=sale_price,
        payment_method="cash",
        created_by_user_id=None,
        purchased_at=datetime.now(UTC),
        external_reference=None,
        external_id=None,
        idempotency_key=None,
    )


def _package_read_by_id(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service_id: UUID,
    package_id: UUID,
):
    rows = list_patient_packages(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service_id,
        usable_only=False,
    )
    for row in rows:
        if str(row.id) == str(package_id):
            return row
    raise RuntimeError(f"Package {package_id} was not found in package reads.")


def _package_state(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service_id: UUID,
    package_id: UUID,
) -> dict[str, Any]:
    row = _package_read_by_id(
        db,
        workspace=workspace,
        patient=patient,
        service_id=service_id,
        package_id=package_id,
    )
    return {
        "id": str(row.id),
        "effective_status": row.effective_status,
        "sessions_purchased": int(row.sessions_purchased),
        "sessions_reserved": int(row.sessions_reserved),
        "sessions_consumed": int(row.sessions_consumed),
        "sessions_remaining": int(row.sessions_remaining),
    }


def _manual_appointment(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service: Service,
    branch_id: UUID,
    doctor_id: UUID,
    slot,
    status: str = "pending",
    day_shift: int = 0,
) -> Appointment:
    shift = timedelta(days=day_shift)
    appointment = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch_id,
        doctor_id=doctor_id,
        service_id=service.id,
        lead_id=None,
        created_by_user_id=None,
        status=status,
        source="ai",
        start_at=slot.start_at + shift,
        end_at=slot.end_at + shift,
        busy_start_at=slot.start_at + shift,
        busy_end_at=slot.end_at + shift,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        customer_note="package-booking-tracking-fixture",
    )
    db.add(appointment)
    db.flush()
    return appointment


def _reserved_fixture(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    package,
    service: Service,
    branch_id: UUID,
    doctor_id: UUID,
    slot,
) -> Appointment:
    appointment = _manual_appointment(
        db,
        workspace=workspace,
        patient=patient,
        service=service,
        branch_id=branch_id,
        doctor_id=doctor_id,
        slot=slot,
        status="pending",
    )
    reserve_package_usage(
        db,
        appointment=appointment,
        package=package,
        sessions=1,
        actor_user_id=None,
    )
    db.flush()
    return appointment


def _booking_context(
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    service: Service,
    primary_branch: dict[str, Any],
):
    doctor, _, timezone_name, slot = _find_doctor_and_slot(
        db,
        workspace,
        catalog,
        branch_id=UUID(str(primary_branch["id"])),
        service_id=service.id,
    )
    date_text, time_text = _format_local_slot(slot, timezone_name)
    return doctor, slot, date_text, time_text


def _case_booking_reserves_one(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm 6 Sessions",
            sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, catalog, underarm, primary_branch
        )
        before_ids = {row.id for row in _appointments_for_patient(db, workspace, patient)}
        _, turn = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message=(
                f"عايز احجز ليزر ابط مع {_doctor_name(doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            ),
            conversation_id=None,
            turn_number=1,
        )
        turns.append(turn)

        created = [
            row
            for row in _appointments_for_patient(db, workspace, patient)
            if row.id not in before_ids
        ]
        appointment = created[-1] if created else None
        state = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        ok = (
            len(created) == 1
            and appointment is not None
            and appointment.patient_package_id == package.id
            and appointment.billing_context == "package_prepaid"
            and appointment.payment_status == "paid"
            and state["sessions_reserved"] == 1
            and state["sessions_consumed"] == 0
            and state["sessions_remaining"] == 5
        )
        return CaseResult(
            name="package_booking_reserves_one_session",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "package_id": str(package.id),
                "appointment_id": str(appointment.id) if appointment else None,
                "billing_context": appointment.billing_context if appointment else None,
                "payment_status": appointment.payment_status if appointment else None,
                "package_state": state,
            },
            error=None if ok else (
                "Expected same-service booking to reserve exactly one package session "
                "and mark the appointment package_prepaid."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_booking_reserves_one_session",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_booking_then_remaining_followup(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm 6 Sessions",
            sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, catalog, underarm, primary_branch
        )
        before_ids = {row.id for row in _appointments_for_patient(db, workspace, patient)}

        first, turn1 = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message=(
                f"عايز احجز ليزر ابط مع {_doctor_name(doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            ),
            conversation_id=None,
            turn_number=1,
        )
        turns.append(turn1)

        second, turn2 = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message="تمام، فاضلي كام جلسة في الباكدج بعد الحجز ده؟",
            conversation_id=first.conversation_id,
            turn_number=2,
        )
        turns.append(turn2)

        created = [
            row
            for row in _appointments_for_patient(db, workspace, patient)
            if row.id not in before_ids
        ]
        state = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        ok = (
            len(created) == 1
            and state["sessions_reserved"] == 1
            and state["sessions_remaining"] == 5
            and "5" in _normalized_numbers(second.reply)
        )
        return CaseResult(
            name="package_booking_followup_reports_remaining",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "appointments_created": len(created),
                "package_state": state,
                "followup_reply": second.reply,
            },
            error=None if ok else (
                "After reserving one session from a 6-session package, follow-up "
                "tracking must report 5 sessions remaining."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_booking_followup_reports_remaining",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_followup_does_not_double_reserve(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm 6 Sessions",
            sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, catalog, underarm, primary_branch
        )
        before_ids = {row.id for row in _appointments_for_patient(db, workspace, patient)}

        first, turn1 = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message=(
                f"عايز احجز ليزر ابط مع {_doctor_name(doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            ),
            conversation_id=None,
            turn_number=1,
        )
        turns.append(turn1)

        _, turn2 = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message="وسعر الجلسة كام؟",
            conversation_id=first.conversation_id,
            turn_number=2,
        )
        turns.append(turn2)

        created = [
            row
            for row in _appointments_for_patient(db, workspace, patient)
            if row.id not in before_ids
        ]
        state = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        ok = (
            len(created) == 1
            and state["sessions_reserved"] == 1
            and state["sessions_consumed"] == 0
            and state["sessions_remaining"] == 5
        )
        return CaseResult(
            name="package_followup_does_not_double_reserve",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "appointments_created": len(created),
                "package_state": state,
            },
            error=None if ok else (
                "A non-booking follow-up must not create another appointment or "
                "reserve a second package session."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_followup_does_not_double_reserve",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_different_service_not_used(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    full_body: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        other_package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=full_body,
            name="Full Body 5 Sessions",
            sessions=5,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, catalog, underarm, primary_branch
        )
        before_ids = {row.id for row in _appointments_for_patient(db, workspace, patient)}

        _, turn = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message=(
                f"عايز احجز ليزر ابط مع {_doctor_name(doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            ),
            conversation_id=None,
            turn_number=1,
        )
        turns.append(turn)

        created = [
            row
            for row in _appointments_for_patient(db, workspace, patient)
            if row.id not in before_ids
        ]
        appointment = created[-1] if created else None
        state = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=full_body.id,
            package_id=other_package.id,
        )
        ok = (
            len(created) == 1
            and appointment is not None
            and appointment.service_id == underarm.id
            and appointment.patient_package_id is None
            and appointment.billing_context == "standard"
            and state["sessions_remaining"] == 5
        )
        return CaseResult(
            name="different_service_package_is_not_used",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "appointment_id": str(appointment.id) if appointment else None,
                "billing_context": appointment.billing_context if appointment else None,
                "other_package_state": state,
            },
            error=None if ok else "A package must never cover a different service_id.",
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="different_service_package_is_not_used",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_cancel_releases(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm Cancel Test",
            sessions=6,
        )
        doctor, _, _, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=underarm.id,
        )
        appointment = _reserved_fixture(
            db,
            workspace=workspace,
            patient=patient,
            package=package,
            service=underarm,
            branch_id=UUID(str(primary_branch["id"])),
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
        )
        before = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        appointment.status = "cancelled"
        release_package_usage(
            db,
            appointment=appointment,
            actor_user_id=None,
            reason="customer_cancelled",
        )
        db.flush()
        after = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        ok = (
            before["sessions_reserved"] == 1
            and before["sessions_remaining"] == 5
            and after["sessions_reserved"] == 0
            and after["sessions_consumed"] == 0
            and after["sessions_remaining"] == 6
        )
        return CaseResult(
            name="package_cancel_releases_reserved_session",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            details={"before": before, "after": after},
            error=None if ok else "Cancellation must return the reserved package session.",
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_cancel_releases_reserved_session",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_no_show_releases(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm No Show Test",
            sessions=6,
        )
        doctor, _, _, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=underarm.id,
        )
        appointment = _reserved_fixture(
            db,
            workspace=workspace,
            patient=patient,
            package=package,
            service=underarm,
            branch_id=UUID(str(primary_branch["id"])),
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
        )
        appointment.status = "no_show"
        release_package_usage(
            db,
            appointment=appointment,
            actor_user_id=None,
            reason="no_show_treated_as_cancelled",
        )
        db.flush()
        state = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        ok = (
            state["sessions_reserved"] == 0
            and state["sessions_consumed"] == 0
            and state["sessions_remaining"] == 6
        )
        return CaseResult(
            name="package_no_show_returns_session",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            details={"package_state": state},
            error=None if ok else (
                "No-show is treated like cancelled and must not consume a package session."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_no_show_returns_session",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_reschedule_transfers(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm Reschedule Test",
            sessions=6,
        )
        doctor, _, _, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=underarm.id,
        )
        old_appointment = _reserved_fixture(
            db,
            workspace=workspace,
            patient=patient,
            package=package,
            service=underarm,
            branch_id=UUID(str(primary_branch["id"])),
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
        )
        before = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        new_appointment = _manual_appointment(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            branch_id=UUID(str(primary_branch["id"])),
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
            status="pending",
            day_shift=1,
        )
        transfer_package_usage(
            db,
            from_appointment=old_appointment,
            to_appointment=new_appointment,
        )
        old_appointment.status = "rescheduled"
        db.flush()
        after = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        ok = (
            before["sessions_reserved"] == 1
            and before["sessions_remaining"] == 5
            and after["sessions_reserved"] == 1
            and after["sessions_consumed"] == 0
            and after["sessions_remaining"] == 5
            and new_appointment.patient_package_id == package.id
            and new_appointment.billing_context == "package_prepaid"
        )
        return CaseResult(
            name="package_reschedule_transfers_same_reservation",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            details={
                "old_appointment_id": str(old_appointment.id),
                "new_appointment_id": str(new_appointment.id),
                "before": before,
                "after": after,
                "new_billing_context": new_appointment.billing_context,
            },
            error=None if ok else (
                "Reschedule must transfer the existing package reservation without "
                "deducting a second session."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_reschedule_transfers_same_reservation",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_completion_consumes(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    try:
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm Completion Test",
            sessions=6,
        )
        doctor, _, _, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=underarm.id,
        )
        appointment = _reserved_fixture(
            db,
            workspace=workspace,
            patient=patient,
            package=package,
            service=underarm,
            branch_id=UUID(str(primary_branch["id"])),
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
        )
        before = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        appointment.status = "completed"
        appointment.completed_at = slot.end_at
        consume_package_usage(
            db,
            appointment=appointment,
            used_at=slot.end_at,
            actor_user_id=None,
        )
        db.flush()
        after = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=package.id,
        )
        ok = (
            before["sessions_reserved"] == 1
            and before["sessions_consumed"] == 0
            and after["sessions_reserved"] == 0
            and after["sessions_consumed"] == 1
            and after["sessions_remaining"] == 5
        )
        return CaseResult(
            name="package_completed_appointment_consumes_session",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            details={"before": before, "after": after},
            error=None if ok else (
                "Completion must convert one reserved package session into consumed."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_completed_appointment_consumes_session",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_refund_after_one_consumed(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    turns: list[TurnResult] = []
    try:
        sale_price = int(underarm.price_minor) * 4  # buy 5 for the price of 4
        package = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm Refund Test",
            sessions=5,
            sale_price_minor=sale_price,
        )
        doctor, _, _, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=underarm.id,
        )
        appointment = _reserved_fixture(
            db,
            workspace=workspace,
            patient=patient,
            package=package,
            service=underarm,
            branch_id=UUID(str(primary_branch["id"])),
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
        )
        appointment.status = "completed"
        appointment.completed_at = slot.end_at
        consume_package_usage(
            db,
            appointment=appointment,
            used_at=slot.end_at,
            actor_user_id=None,
        )
        db.flush()

        quote = _package_refund_quote_payload(
            db=db,
            workspace_id=workspace.id,
            patient_id=patient.id,
            service_id=str(underarm.id),
        )
        expected_refund_minor = sale_price - int(underarm.price_minor)

        response, turn = _send_turn(
            db=db,
            workspace=workspace,
            patient=patient,
            message="لو لغيت باكدج ليزر الإبط دلوقتي هيرجعلي كام؟",
            conversation_id=None,
            turn_number=1,
        )
        turns.append(turn)

        actual = quote.get("quote") if isinstance(quote, dict) else None
        expected_egp = expected_refund_minor // 100
        ok = (
            isinstance(actual, dict)
            and int(actual.get("consumed_sessions") or -1) == 1
            and int(actual.get("refundable_minor") or -1) == expected_refund_minor
            and str(expected_egp) in _normalized_numbers(response.reply)
        )
        return CaseResult(
            name="package_refund_after_one_consumed_session",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            details={
                "standalone_session_price_minor": int(underarm.price_minor),
                "sale_price_minor": sale_price,
                "expected_refund_minor": expected_refund_minor,
                "quote": quote,
            },
            error=None if ok else (
                "Refund must deduct each consumed session at the standalone price, "
                "not at the discounted package rate."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="package_refund_after_one_consumed_session",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_active_rejected_then_exhausted_allows_new(
    *,
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    patient,
    underarm: Service,
    **_: Any,
) -> CaseResult:
    started = perf_counter()
    try:
        first = _create_package(
            db,
            workspace=workspace,
            patient=patient,
            service=underarm,
            name="Underarm Current Package",
            sessions=1,
        )

        rejected_while_active = False
        active_error = None
        try:
            _create_package(
                db,
                workspace=workspace,
                patient=patient,
                service=underarm,
                name="Underarm Too Early Package",
                sessions=3,
            )
        except Exception as exc:  # noqa: BLE001
            rejected_while_active = True
            active_error = f"{type(exc).__name__}: {exc}"

        doctor, _, _, slot = _find_doctor_and_slot(
            db,
            workspace,
            catalog,
            branch_id=UUID(str(primary_branch["id"])),
            service_id=underarm.id,
        )
        appointment = _reserved_fixture(
            db,
            workspace=workspace,
            patient=patient,
            package=first,
            service=underarm,
            branch_id=UUID(str(primary_branch["id"])),
            doctor_id=UUID(str(doctor["id"])),
            slot=slot,
        )
        appointment.status = "completed"
        appointment.completed_at = slot.end_at
        consume_package_usage(
            db,
            appointment=appointment,
            used_at=slot.end_at,
            actor_user_id=None,
        )
        db.flush()

        first_state = _package_state(
            db,
            workspace=workspace,
            patient=patient,
            service_id=underarm.id,
            package_id=first.id,
        )

        second = None
        allowed_after_exhausted = False
        second_error = None
        try:
            second = _create_package(
                db,
                workspace=workspace,
                patient=patient,
                service=underarm,
                name="Underarm Next Package",
                sessions=3,
            )
            allowed_after_exhausted = second is not None
        except Exception as exc:  # noqa: BLE001
            second_error = f"{type(exc).__name__}: {exc}"

        ok = (
            rejected_while_active
            and first_state["sessions_remaining"] == 0
            and allowed_after_exhausted
            and second is not None
        )
        return CaseResult(
            name="same_service_new_package_only_after_current_exhausted",
            status="PASS" if ok else "FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            details={
                "first_package_id": str(first.id),
                "rejected_while_active": rejected_while_active,
                "active_rejection": active_error,
                "first_package_state_after_completion": first_state,
                "allowed_after_exhausted": allowed_after_exhausted,
                "second_package_id": str(second.id) if second else None,
                "second_creation_error": second_error,
            },
            error=None if ok else (
                "A second same-service package must be rejected while the current "
                "package has sessions remaining, then allowed once it is exhausted."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            name="same_service_new_package_only_after_current_exhausted",
            status="FAIL",
            duration_ms=int((perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )


CASES: list[Callable[..., CaseResult]] = [
    _case_booking_reserves_one,
    _case_booking_then_remaining_followup,
    _case_followup_does_not_double_reserve,
    _case_different_service_not_used,
    _case_cancel_releases,
    _case_no_show_releases,
    _case_reschedule_transfers,
    _case_completion_consumes,
    _case_refund_after_one_consumed,
    _case_active_rejected_then_exhausted_allows_new,
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run 10 focused package booking + tracking tests."
    )
    parser.add_argument("--workspace-slug", default="tia-demo")
    parser.add_argument("--workspace-id", type=UUID, default=None)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument(
        "--report",
        default="artifacts/agent-package-booking-tracking-10tests.json",
    )
    return parser.parse_args()


def _load_context(db: Session, args: argparse.Namespace):
    workspace = (
        db.scalar(select(Workspace).where(Workspace.id == args.workspace_id))
        if args.workspace_id
        else db.scalar(select(Workspace).where(Workspace.slug == args.workspace_slug))
    )
    if workspace is None:
        raise RuntimeError("Workspace not found.")

    catalog = build_clinic_catalog(db, workspace)
    primary_branch = _primary_branch_row(workspace, catalog)

    underarm_row = _catalog_row(catalog, "services", "ليزر إزالة الشعر - إبط")
    full_body_row = _catalog_row(
        catalog,
        "services",
        "ليزر إزالة الشعر - جسم كامل سيدات",
    )
    underarm = _service(db, workspace, UUID(str(underarm_row["id"])))
    full_body = _service(db, workspace, UUID(str(full_body_row["id"])))

    patients = _fixture_patients(db, workspace)
    preferred = [
        "busy-evening",
        "pending-new-cairo",
        "injectables",
        "cancelled-slot",
        "history",
    ]
    patient = next((patients[key] for key in preferred if patients.get(key) is not None), None)
    if patient is None:
        raise RuntimeError(
            "No realistic fixture patient was found. Run seed_realistic_aesthetic_clinic.py first."
        )

    return workspace, catalog, primary_branch, underarm, full_body, patient


def main() -> int:
    args = parse_args()

    if str(settings.environment or "").strip().lower() == "production":
        print("Refusing to run package tests in production.", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()

    bootstrap = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    report: Report | None = None
    exit_code = 1

    try:
        workspace, _, _, _, _, _ = _load_context(bootstrap, args)
        report = Report(
            started_at=datetime.now(UTC).isoformat(),
            workspace_id=str(workspace.id),
            workspace_slug=workspace.slug,
            rollback=not args.keep_data,
        )
        bootstrap.close()

        for case_fn in CASES:
            # Isolate each package scenario. Agent/service commits stay below this
            # connection-level savepoint; the savepoint is rolled back after the case.
            case_savepoint = connection.begin_nested()
            case_db = Session(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                (
                    workspace,
                    catalog,
                    primary_branch,
                    underarm,
                    full_body,
                    patient,
                ) = _load_context(case_db, args)

                result = case_fn(
                    db=case_db,
                    workspace=workspace,
                    catalog=catalog,
                    primary_branch=primary_branch,
                    patient=patient,
                    underarm=underarm,
                    full_body=full_body,
                )
            except Exception as exc:  # noqa: BLE001
                result = CaseResult(
                    name=getattr(case_fn, "__name__", "unknown_case"),
                    status="FAIL",
                    duration_ms=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                case_db.close()
                if case_savepoint.is_active:
                    case_savepoint.rollback()

            report.results.append(result)
            print(f"[{result.status}] {result.name} ({result.duration_ms} ms)")
            if result.error:
                print(f"       {result.error}")

        exit_code = 1 if report.counts().get("FAIL", 0) else 0

    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if report is not None:
            report.results.append(
                CaseResult(
                    name="suite_exception",
                    status="FAIL",
                    duration_ms=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        exit_code = 1

    finally:
        try:
            bootstrap.close()
        except Exception:
            pass

        if args.keep_data:
            outer.commit()
        else:
            outer.rollback()
        connection.close()
        engine.dispose()

    if report is not None:
        path = Path(args.report)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            **{k: v for k, v in asdict(report).items() if k != "results"},
            "counts": report.counts(),
            "results": [asdict(row) for row in report.results],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print("\nSummary:", json.dumps(report.counts(), ensure_ascii=False))
        print(f"Report: {path}")
        print(f"Database writes rolled back: {'no' if args.keep_data else 'yes'}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
