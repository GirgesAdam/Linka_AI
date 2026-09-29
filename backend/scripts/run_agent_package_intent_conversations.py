from __future__ import annotations

"""18 focused conversations for package-vs-single-session understanding.

Test-oracle rule: no keyword, regex, or phrase-list routing is used. Intent
assertions rely on structured LLM output plus persisted database effects.

This suite intentionally avoids re-testing package lifecycle cases that already passed
(cancel/no-show/reschedule/consume). It focuses on the LLM + booking flow deciding:

- Is the customer asking for one normal appointment or for a package?
- If the customer already has a package for the same service and does NOT mention it,
  does booking automatically use that package and tell them what remains?
- If they explicitly say "not from my package", is that preference respected?
- If they have a package for one service but book another service, is the other service
  kept completely separate?
- If a first-time customer has no package, can the system distinguish "one session"
  from "package / 6 sessions / full course" language?
- If the customer changes their mind mid-conversation, does the flow follow the newest
  intent without accidentally writing an appointment?

The runner also records the unified semantic interpreter's first-turn capabilities
where possible, so failures can be separated into:
1) LLM intent-understanding failures, versus
2) execution / package-application failures.

All cases run in isolated savepoints and are rolled back.
"""

import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import UUID
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.agents.turn_interpreter import interpret_customer_turn
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.service import Service
from app.models.workspace import Workspace
from app.services.patient_packages import create_patient_package, list_patient_packages

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
class SemanticProbe:
    message: str
    capabilities: list[str] = field(default_factory=list)
    flow_signal: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    service_id: str | None = None
    confidence: float | None = None
    error: str | None = None


@dataclass
class CaseResult:
    name: str
    status: str
    duration_ms: int
    semantic: list[SemanticProbe] = field(default_factory=list)
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
        result = {"PASS": 0, "FAIL": 0}
        for row in self.results:
            result[row.status] = result.get(row.status, 0) + 1
        return result


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
def _normalized(text: str | None) -> str:
    return (text or "").translate(_ARABIC_DIGITS).lower()

def _primary_branch_row(workspace: Workspace, catalog: dict[str, Any]) -> dict[str, Any]:
    branches = list(catalog.get("branches") or [])
    if workspace.primary_branch_id is not None:
        for row in branches:
            if str(row.get("id")) == str(workspace.primary_branch_id):
                return row
        raise RuntimeError("Primary branch was not found in the active catalog.")
    if len(branches) == 1:
        return branches[0]
    raise RuntimeError("This suite expects Linka's current single-branch product model.")


def _doctor_name(row: dict[str, Any]) -> str:
    return str(
        row.get("name")
        or row.get("display_name")
        or row.get("full_name")
        or ""
    ).strip()


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


def _booking_context(
    db: Session,
    workspace: Workspace,
    catalog: dict[str, Any],
    primary_branch: dict[str, Any],
    service: Service,
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


def _appointments(db: Session, workspace: Workspace, patient) -> list[Appointment]:
    return list(_appointments_for(db, workspace, patient))


def _send_turn(
    *,
    db: Session,
    workspace: Workspace,
    patient,
    message: str,
    conversation_id: UUID | None,
    turn_number: int,
) -> tuple[Any, TurnResult]:
    response, duration_ms = _send(
        db,
        workspace,
        patient,
        message,
        conversation_id,
    )
    rows = _appointments(db, workspace, patient)
    return response, TurnResult(
        turn=turn_number,
        message=message,
        reply=response.reply,
        duration_ms=duration_ms,
        appointment_ids_seen=[str(row.id) for row in rows],
    )


def _semantic_probe(
    *,
    workspace: Workspace,
    catalog: dict[str, Any],
    message: str,
) -> SemanticProbe:
    try:
        timezone_name = (workspace.timezone or "Africa/Cairo").strip()
        local_now = datetime.now(ZoneInfo(timezone_name))
        decision = interpret_customer_turn(
            flow=None,
            history=[HumanMessage(content=message)],
            timezone_name=timezone_name,
            local_now=local_now,
            clinic_catalog=catalog,
        )
        hints = decision.entity_hints
        return SemanticProbe(
            message=message,
            capabilities=list(decision.capabilities),
            flow_signal=str(decision.flow_signal),
            risk_flags=list(decision.risk_flags),
            service_id=str(hints.service_id) if hints.service_id else None,
            confidence=float(decision.confidence),
        )
    except Exception as exc:  # noqa: BLE001
        return SemanticProbe(
            message=message,
            error=f"{type(exc).__name__}: {exc}",
        )


def _create_package(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service: Service,
    name: str = "Intent Test Package",
    sessions: int = 6,
):
    sale_price = max(
        int(service.price_minor),
        int(service.price_minor) * max(1, sessions - 1),
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


def _package_rows(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service_id: UUID,
):
    return list_patient_packages(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service_id,
        usable_only=False,
    )


def _usable_package_rows(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service_id: UUID,
):
    return list_patient_packages(
        db,
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service_id,
        usable_only=True,
    )


def _package_state(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service_id: UUID,
    package_id: UUID,
) -> dict[str, Any]:
    for row in _package_rows(
        db,
        workspace=workspace,
        patient=patient,
        service_id=service_id,
    ):
        if str(row.id) == str(package_id):
            return {
                "id": str(row.id),
                "effective_status": row.effective_status,
                "sessions_purchased": int(row.sessions_purchased),
                "sessions_reserved": int(row.sessions_reserved),
                "sessions_consumed": int(row.sessions_consumed),
                "sessions_remaining": int(row.sessions_remaining),
            }
    raise RuntimeError(f"Package not found: {package_id}")


def _new_appointments(
    db: Session,
    workspace: Workspace,
    patient,
    before_ids: set[UUID],
) -> list[Appointment]:
    return [
        row for row in _appointments(db, workspace, patient)
        if row.id not in before_ids
    ]


def _new_packages_count(
    db: Session,
    *,
    workspace: Workspace,
    patient,
    service_id: UUID,
    baseline_ids: set[str],
) -> int:
    rows = _package_rows(
        db,
        workspace=workspace,
        patient=patient,
        service_id=service_id,
    )
    return sum(1 for row in rows if str(row.id) not in baseline_ids)


def _appointment_snapshot(row: Appointment | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "service_id": str(row.service_id),
        "patient_package_id": str(row.patient_package_id) if row.patient_package_id else None,
        "billing_context": row.billing_context,
        "payment_status": row.payment_status,
        "status": row.status,
    }


def _case(
    name: str,
    started: float,
    *,
    ok: bool,
    semantic: list[SemanticProbe] | None = None,
    turns: list[TurnResult] | None = None,
    details: dict[str, Any] | None = None,
    error: str,
) -> CaseResult:
    return CaseResult(
        name=name,
        status="PASS" if ok else "FAIL",
        duration_ms=int((perf_counter() - started) * 1000),
        semantic=semantic or [],
        turns=turns or [],
        details=details or {},
        error=None if ok else error,
    )


# ---------------------------------------------------------------------------
# Existing package: same service
# ---------------------------------------------------------------------------

def case_existing_package_implicit_exact_booking_mentions_remaining(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        package = _create_package(
            db, workspace=workspace, patient=patient, service=underarm,
            name="Underarm Existing Package", sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        message = (
            f"عايز احجز ليزر ابط مع {_doctor_name(doctor)} "
            f"يوم {date_text} الساعة {time_text}"
        )
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)

        created = _new_appointments(db, workspace, patient, before_ids)
        appt = created[-1] if created else None
        state = _package_state(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, package_id=package.id,
        )
        reply = _normalized(response.reply)
        ok = (
            len(created) == 1
            and appt is not None
            and appt.patient_package_id == package.id
            and appt.billing_context == "package_prepaid"
            and state["sessions_remaining"] == 5
            and "5" in reply
        )
        return _case(
            "existing_package_implicit_same_service_booking_mentions_remaining",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointment": _appointment_snapshot(appt),
                "package_state": state,
                "reply": response.reply,
            },
            error=(
                "When the customer already has a same-service package and books normally "
                "without mentioning it, Linka should use that package automatically and the "
                "booking reply should tell them the remaining session count."
            ),
        )
    except Exception as exc:
        return _case(
            "existing_package_implicit_same_service_booking_mentions_remaining",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_existing_package_options_then_select_mentions_remaining(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        package = _create_package(
            db, workspace=workspace, patient=patient, service=underarm,
            name="Underarm Existing Package", sessions=6,
        )
        doctor, _, date_text, _ = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        message1 = (
            f"عايز احجز ليزر ابط مع {_doctor_name(doctor)} يوم {date_text}، "
            "وريني المواعيد المتاحة"
        )
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message1
        ))
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        first, turn1 = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message1,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn1)
        after_options = _new_appointments(db, workspace, patient, before_ids)

        second, turn2 = _send_turn(
            db=db, workspace=workspace, patient=patient,
            message="احجز أول ميعاد متاح",
            conversation_id=first.conversation_id, turn_number=2,
        )
        turns.append(turn2)
        created = _new_appointments(db, workspace, patient, before_ids)
        appt = created[-1] if created else None
        state = _package_state(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, package_id=package.id,
        )
        ok = (
            len(after_options) == 0
            and len(created) == 1
            and appt is not None
            and appt.patient_package_id == package.id
            and state["sessions_remaining"] == 5
            and "5" in _normalized(second.reply)
        )
        return _case(
            "existing_package_options_then_select_uses_package_and_mentions_remaining",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointments_after_options": len(after_options),
                "appointment": _appointment_snapshot(appt),
                "package_state": state,
                "final_reply": second.reply,
            },
            error=(
                "Selecting a slot for the same service should use the existing package "
                "and tell the customer how many sessions remain."
            ),
        )
    except Exception as exc:
        return _case(
            "existing_package_options_then_select_uses_package_and_mentions_remaining",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_existing_package_explicit_use(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        package = _create_package(
            db, workspace=workspace, patient=patient, service=underarm,
            name="Underarm Existing Package", sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        message = (
            f"عندي باكدج ليزر ابط وعايز احجز منها جلسة مع {_doctor_name(doctor)} "
            f"يوم {date_text} الساعة {time_text}"
        )
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created = _new_appointments(db, workspace, patient, before_ids)
        appt = created[-1] if created else None
        state = _package_state(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, package_id=package.id,
        )
        caps = set(semantic[0].capabilities)
        ok = (
            len(created) == 1
            and appt is not None
            and appt.patient_package_id == package.id
            and state["sessions_remaining"] == 5
            and "package_information" in caps
            and "appointment_creation" in caps
        )
        return _case(
            "existing_package_explicit_use_is_understood_and_applied",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointment": _appointment_snapshot(appt),
                "package_state": state,
                "reply": response.reply,
            },
            error=(
                "Explicit 'book from my package' should be understood as both package use "
                "and appointment creation, then reserve one session from that package."
            ),
        )
    except Exception as exc:
        return _case(
            "existing_package_explicit_use_is_understood_and_applied",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_existing_package_explicit_standard_override(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        package = _create_package(
            db, workspace=workspace, patient=patient, service=underarm,
            name="Underarm Existing Package", sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        message = (
            f"عندي باكدج ليزر ابط بس المرة دي عايز الجلسة عادية ومش من الباكدج، "
            f"احجزهالي مع {_doctor_name(doctor)} يوم {date_text} الساعة {time_text}"
        )
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created = _new_appointments(db, workspace, patient, before_ids)
        appt = created[-1] if created else None
        state = _package_state(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, package_id=package.id,
        )
        ok = (
            len(created) == 1
            and appt is not None
            and appt.patient_package_id is None
            and appt.billing_context == "standard"
            and state["sessions_reserved"] == 0
            and state["sessions_remaining"] == 6
        )
        return _case(
            "existing_package_explicit_standard_session_does_not_use_package",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointment": _appointment_snapshot(appt),
                "package_state": state,
                "reply": response.reply,
            },
            error=(
                "If the customer explicitly says this appointment is NOT from the package, "
                "Linka must respect that and leave package sessions untouched."
            ),
        )
    except Exception as exc:
        return _case(
            "existing_package_explicit_standard_session_does_not_use_package",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Existing package: customer wants something else
# ---------------------------------------------------------------------------

def case_existing_package_other_service_full_conversation(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm, full_body = ctx["underarm"], ctx["full_body"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        package = _create_package(
            db, workspace=workspace, patient=patient, service=underarm,
            name="Underarm Existing Package", sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], full_body
        )
        message1 = "عندي باكدج ليزر ابط شغالة، بس عايز احجز حاجة تانية: ليزر جسم كامل سيدات"
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message1
        ))
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        first, turn1 = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message1,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn1)

        message2 = (
            f"احجزهالي مع {_doctor_name(doctor)} يوم {date_text} الساعة {time_text}"
        )
        second, turn2 = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message2,
            conversation_id=first.conversation_id, turn_number=2,
        )
        turns.append(turn2)

        created = _new_appointments(db, workspace, patient, before_ids)
        appt = created[-1] if created else None
        state = _package_state(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, package_id=package.id,
        )
        ok = (
            len(created) == 1
            and appt is not None
            and appt.service_id == full_body.id
            and appt.patient_package_id is None
            and appt.billing_context == "standard"
            and state["sessions_reserved"] == 0
            and state["sessions_remaining"] == 6
        )
        return _case(
            "existing_package_customer_books_different_service_full_conversation",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointment": _appointment_snapshot(appt),
                "underarm_package_state": state,
                "turn_1_reply": first.reply,
                "turn_2_reply": second.reply,
            },
            error=(
                "A customer may have a package for one service and book another service. "
                "The new service must stay standard and the existing package must remain untouched."
            ),
        )
    except Exception as exc:
        return _case(
            "existing_package_customer_books_different_service_full_conversation",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_existing_package_other_service_not_mentioned(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm, full_body = ctx["underarm"], ctx["full_body"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        package = _create_package(
            db, workspace=workspace, patient=patient, service=underarm,
            name="Underarm Existing Package", sessions=6,
        )
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], full_body
        )
        message = (
            f"عايز احجز ليزر جسم كامل سيدات مع {_doctor_name(doctor)} "
            f"يوم {date_text} الساعة {time_text}"
        )
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created = _new_appointments(db, workspace, patient, before_ids)
        appt = created[-1] if created else None
        state = _package_state(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, package_id=package.id,
        )
        ok = (
            len(created) == 1
            and appt is not None
            and appt.service_id == full_body.id
            and appt.patient_package_id is None
            and state["sessions_remaining"] == 6
        )
        return _case(
            "existing_package_unmentioned_different_service_stays_standard",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointment": _appointment_snapshot(appt),
                "underarm_package_state": state,
                "reply": response.reply,
            },
            error=(
                "An unrelated existing package must not leak into a booking for another service, "
                "even when the customer never mentions the package."
            ),
        )
    except Exception as exc:
        return _case(
            "existing_package_unmentioned_different_service_stays_standard",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_existing_package_wants_new_package_for_other_service(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm, full_body = ctx["underarm"], ctx["full_body"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        existing = _create_package(
            db, workspace=workspace, patient=patient, service=underarm,
            name="Underarm Existing Package", sessions=6,
        )
        baseline_full_body = {
            str(row.id) for row in _package_rows(
                db, workspace=workspace, patient=patient, service_id=full_body.id
            )
        }
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        message = "عندي باكدج ليزر ابط، وعايز أعمل باكدج ليزر جسم كامل سيدات كمان"
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created_appts = _new_appointments(db, workspace, patient, before_ids)
        created_packages = _new_packages_count(
            db, workspace=workspace, patient=patient,
            service_id=full_body.id, baseline_ids=baseline_full_body,
        )
        state = _package_state(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, package_id=existing.id,
        )
        caps = set(semantic[0].capabilities)
        ok = (
            len(created_appts) == 0
            and created_packages == 0
            and state["sessions_remaining"] == 6
            and "package_information" in caps
            and "appointment_creation" not in caps
        )
        return _case(
            "existing_package_customer_requests_new_package_for_other_service",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "new_appointments": len(created_appts),
                "new_full_body_packages": created_packages,
                "existing_underarm_package_state": state,
                "reply": response.reply,
            },
            error=(
                "Requesting a new package for another service must not be mistaken for "
                "a single appointment, and must not consume the existing package."
            ),
        )
    except Exception as exc:
        return _case(
            "existing_package_customer_requests_new_package_for_other_service",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# No existing package: single-session intent
# ---------------------------------------------------------------------------

def case_no_package_explicit_single_session_exact(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        message = (
            f"أنا أول مرة، عايز جلسة واحدة بس ليزر ابط مع {_doctor_name(doctor)} "
            f"يوم {date_text} الساعة {time_text}"
        )
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created = _new_appointments(db, workspace, patient, before_ids)
        appt = created[-1] if created else None
        caps = set(semantic[0].capabilities)
        ok = (
            len(created) == 1
            and appt is not None
            and appt.patient_package_id is None
            and appt.billing_context == "standard"
            and "appointment_creation" in caps
        )
        return _case(
            "no_package_explicit_single_session_is_standard_booking",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointment": _appointment_snapshot(appt),
                "reply": response.reply,
            },
            error=(
                "A first-time customer explicitly asking for one session should be "
                "understood as a normal appointment, not package intent."
            ),
        )
    except Exception as exc:
        return _case(
            "no_package_explicit_single_session_is_standard_booking",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_no_package_single_session_three_turns(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        before_ids = {row.id for row in _appointments(db, workspace, patient)}

        message1 = "عايز أبدأ ليزر ابط"
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message1
        ))
        first, turn1 = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message1,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn1)
        after1 = _new_appointments(db, workspace, patient, before_ids)

        second, turn2 = _send_turn(
            db=db, workspace=workspace, patient=patient,
            message="جلسة واحدة بس مش باكدج",
            conversation_id=first.conversation_id, turn_number=2,
        )
        turns.append(turn2)
        after2 = _new_appointments(db, workspace, patient, before_ids)

        third, turn3 = _send_turn(
            db=db, workspace=workspace, patient=patient,
            message=(
                f"مع {_doctor_name(doctor)} يوم {date_text} الساعة {time_text}"
            ),
            conversation_id=second.conversation_id, turn_number=3,
        )
        turns.append(turn3)
        final = _new_appointments(db, workspace, patient, before_ids)
        appt = final[-1] if final else None

        ok = (
            len(after1) == 0
            and len(after2) == 0
            and len(final) == 1
            and appt is not None
            and appt.patient_package_id is None
            and appt.billing_context == "standard"
        )
        return _case(
            "no_package_customer_clarifies_single_session_then_books",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "appointments_after_turn1": len(after1),
                "appointments_after_turn2": len(after2),
                "final_appointment": _appointment_snapshot(appt),
                "final_reply": third.reply,
            },
            error=(
                "When the customer clarifies 'one session, not a package', the flow should "
                "keep that intent and create exactly one standard appointment once details arrive."
            ),
        )
    except Exception as exc:
        return _case(
            "no_package_customer_clarifies_single_session_then_books",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# No existing package: package intent
# ---------------------------------------------------------------------------

def _package_intent_no_single_booking_case(
    *,
    case_name: str,
    message: str,
    **ctx,
) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        baseline_packages = {
            str(row.id) for row in _package_rows(
                db, workspace=workspace, patient=patient, service_id=underarm.id
            )
        }
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created_appts = _new_appointments(db, workspace, patient, before_ids)
        created_packages = _new_packages_count(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, baseline_ids=baseline_packages,
        )
        caps = set(semantic[0].capabilities)
        ok = (
            len(created_appts) == 0
            and created_packages == 0
            and "package_information" in caps
            and "appointment_creation" not in caps
        )
        return _case(
            case_name,
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "new_appointments": len(created_appts),
                "new_packages": created_packages,
                "reply": response.reply,
            },
            error=(
                "Package intent must not be mistaken for a single appointment. "
                "The response should remain package-aware and no appointment should be written."
            ),
        )
    except Exception as exc:
        return _case(
            case_name, started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_no_package_says_package(**ctx) -> CaseResult:
    return _package_intent_no_single_booking_case(
        case_name="no_package_customer_says_wants_package",
        message="أنا أول مرة وعايز باكدج ليزر ابط",
        **ctx,
    )


def case_no_package_says_package_six_sessions(**ctx) -> CaseResult:
    return _package_intent_no_single_booking_case(
        case_name="no_package_customer_says_package_six_sessions",
        message="أنا أول مرة وعايز باكدج 6 جلسات ليزر ابط",
        **ctx,
    )


def case_no_package_explicit_subscription_language(**ctx) -> CaseResult:
    return _package_intent_no_single_booking_case(
        case_name="no_package_explicit_subscription_language_is_package_intent",
        message="عايز أشترك في باكدج ليزر ابط وأبدأها قريب",
        **ctx,
    )


def case_no_package_says_six_sessions_without_package_word(**ctx) -> CaseResult:
    return _package_intent_no_single_booking_case(
        case_name="no_package_six_sessions_without_package_word_is_package_intent",
        message="عايز أحجز 6 جلسات ليزر ابط",
        **ctx,
    )


def case_no_package_says_full_course(**ctx) -> CaseResult:
    return _package_intent_no_single_booking_case(
        case_name="no_package_full_course_phrase_is_not_single_session",
        message="عايز أعمل كورس ليزر ابط كامل",
        **ctx,
    )


def case_no_package_asks_package_or_session(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        baseline_packages = {
            str(row.id) for row in _package_rows(
                db, workspace=workspace, patient=patient, service_id=underarm.id
            )
        }
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        message = "أنا أول مرة وعايز أبدأ ليزر ابط، أحجز جلسة واحدة ولا باكدج أحسن؟"
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created_appts = _new_appointments(db, workspace, patient, before_ids)
        created_packages = _new_packages_count(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, baseline_ids=baseline_packages,
        )
        ok = (
            len(created_appts) == 0
            and created_packages == 0
            and "package_information" in set(semantic[0].capabilities)
            and semantic[0].flow_signal == "none"
        )
        return _case(
            "no_package_customer_compares_package_vs_single_session_no_write",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "new_appointments": len(created_appts),
                "new_packages": created_packages,
                "reply": response.reply,
            },
            error=(
                "A customer comparing package vs single session is asking for information/choice, "
                "not authorizing either purchase or appointment creation."
            ),
        )
    except Exception as exc:
        return _case(
            "no_package_customer_compares_package_vs_single_session_no_write",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_no_package_corrects_to_package_mid_conversation(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        baseline_packages = {
            str(row.id) for row in _package_rows(
                db, workspace=workspace, patient=patient, service_id=underarm.id
            )
        }
        before_ids = {row.id for row in _appointments(db, workspace, patient)}

        first, turn1 = _send_turn(
            db=db, workspace=workspace, patient=patient,
            message="عايز ليزر ابط",
            conversation_id=None, turn_number=1,
        )
        turns.append(turn1)

        message2 = "لا أنا قصدي باكدج مش جلسة واحدة"
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message2
        ))
        second, turn2 = _send_turn(
            db=db, workspace=workspace, patient=patient,
            message=message2,
            conversation_id=first.conversation_id, turn_number=2,
        )
        turns.append(turn2)

        created_appts = _new_appointments(db, workspace, patient, before_ids)
        created_packages = _new_packages_count(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, baseline_ids=baseline_packages,
        )
        caps = set(semantic[0].capabilities)
        ok = (
            len(created_appts) == 0
            and created_packages == 0
            and "package_information" in caps
            and "appointment_creation" not in caps
        )
        return _case(
            "no_package_customer_corrects_intent_to_package_mid_conversation",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "new_appointments": len(created_appts),
                "new_packages": created_packages,
                "final_reply": second.reply,
            },
            error=(
                "The latest correction 'I mean a package, not one session' must supersede "
                "any earlier booking interpretation and must not write an appointment."
            ),
        )
    except Exception as exc:
        return _case(
            "no_package_customer_corrects_intent_to_package_mid_conversation",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_no_package_package_then_date_does_not_turn_into_single_booking(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        _, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        baseline_packages = {
            str(row.id) for row in _package_rows(
                db, workspace=workspace, patient=patient, service_id=underarm.id
            )
        }
        before_ids = {row.id for row in _appointments(db, workspace, patient)}

        message1 = "أنا أول مرة وعايز باكدج ليزر ابط"
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message1
        ))
        first, turn1 = _send_turn(
            db=db, workspace=workspace, patient=patient,
            message=message1,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn1)

        second, turn2 = _send_turn(
            db=db, workspace=workspace, patient=patient,
            message=f"ممكن أبدأ يوم {date_text} الساعة {time_text}؟",
            conversation_id=first.conversation_id, turn_number=2,
        )
        turns.append(turn2)

        created_appts = _new_appointments(db, workspace, patient, before_ids)
        created_packages = _new_packages_count(
            db, workspace=workspace, patient=patient,
            service_id=underarm.id, baseline_ids=baseline_packages,
        )
        ok = (
            len(created_appts) == 0
            and created_packages == 0
        )
        return _case(
            "package_intent_followed_by_start_date_does_not_become_single_session",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "new_appointments": len(created_appts),
                "new_packages": created_packages,
                "turn_1_reply": first.reply,
                "turn_2_reply": second.reply,
            },
            error=(
                "After a customer establishes package intent, a later start date/time must "
                "not silently reinterpret that request as one standard appointment."
            ),
        )
    except Exception as exc:
        return _case(
            "package_intent_followed_by_start_date_does_not_become_single_session",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


def case_no_package_claims_existing_package_but_none_exists(**ctx) -> CaseResult:
    started = perf_counter()
    db, workspace, patient = ctx["db"], ctx["workspace"], ctx["patient"]
    underarm = ctx["underarm"]
    turns: list[TurnResult] = []
    semantic: list[SemanticProbe] = []
    try:
        doctor, _, date_text, time_text = _booking_context(
            db, workspace, ctx["catalog"], ctx["primary_branch"], underarm
        )
        before_ids = {row.id for row in _appointments(db, workspace, patient)}
        message = (
            f"عايز احجز جلسة من باكدج ليزر ابط بتاعتي مع {_doctor_name(doctor)} "
            f"يوم {date_text} الساعة {time_text}"
        )
        semantic.append(_semantic_probe(
            workspace=workspace, catalog=ctx["catalog"], message=message
        ))
        response, turn = _send_turn(
            db=db, workspace=workspace, patient=patient, message=message,
            conversation_id=None, turn_number=1,
        )
        turns.append(turn)
        created = _new_appointments(db, workspace, patient, before_ids)
        caps = set(semantic[0].capabilities)
        ok = (
            len(created) == 0
            and "package_information" in caps
        )
        return _case(
            "customer_claims_existing_package_but_none_exists_no_standard_fallback",
            started, ok=ok, semantic=semantic, turns=turns,
            details={
                "new_appointments": len(created),
                "reply": response.reply,
            },
            error=(
                "If the customer explicitly asks to book from a package but no usable package exists, "
                "Linka must not silently create a standard paid appointment."
            ),
        )
    except Exception as exc:
        return _case(
            "customer_claims_existing_package_but_none_exists_no_standard_fallback",
            started, ok=False, semantic=semantic, turns=turns,
            error=f"{type(exc).__name__}: {exc}",
        )


CASES: list[Callable[..., CaseResult]] = [
    # Existing same-service package.
    case_existing_package_implicit_exact_booking_mentions_remaining,
    case_existing_package_options_then_select_mentions_remaining,
    case_existing_package_explicit_use,
    case_existing_package_explicit_standard_override,

    # Existing package but customer wants something else.
    case_existing_package_other_service_full_conversation,
    case_existing_package_other_service_not_mentioned,
    case_existing_package_wants_new_package_for_other_service,

    # No package: clear single-session intent.
    case_no_package_explicit_single_session_exact,
    case_no_package_single_session_three_turns,

    # No package: package intent variants.
    case_no_package_says_package,
    case_no_package_says_package_six_sessions,
    case_no_package_explicit_subscription_language,
    case_no_package_says_six_sessions_without_package_word,
    case_no_package_says_full_course,
    case_no_package_asks_package_or_session,
    case_no_package_corrects_to_package_mid_conversation,
    case_no_package_package_then_date_does_not_turn_into_single_booking,
    case_no_package_claims_existing_package_but_none_exists,
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run focused package-vs-single-session conversation tests."
    )
    parser.add_argument("--workspace-slug", default="tia-demo")
    parser.add_argument("--workspace-id", type=UUID, default=None)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument(
        "--report",
        default="artifacts/agent-package-intent-conversations.json",
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

    # Find a fixture patient with no existing usable package for either target service.
    preferred = [
        "busy-evening",
        "pending-new-cairo",
        "injectables",
        "cancelled-slot",
        "history",
    ]
    patient = None
    for key in preferred:
        candidate = patients.get(key)
        if candidate is None:
            continue
        underarm_usable = _usable_package_rows(
            db, workspace=workspace, patient=candidate, service_id=underarm.id
        )
        full_body_usable = _usable_package_rows(
            db, workspace=workspace, patient=candidate, service_id=full_body.id
        )
        if not underarm_usable and not full_body_usable:
            patient = candidate
            break

    if patient is None:
        raise RuntimeError(
            "Could not find a realistic fixture patient without an active package for "
            "the tested services. Use a clean seeded tia-demo fixture."
        )

    return workspace, catalog, primary_branch, underarm, full_body, patient


def main() -> int:
    args = parse_args()

    if str(settings.environment or "").strip().lower() == "production":
        print("Refusing to run package intent tests in production.", file=sys.stderr)
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
            savepoint = connection.begin_nested()
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
                if savepoint.is_active:
                    savepoint.rollback()

            report.results.append(result)
            print(f"[{result.status}] {result.name} ({result.duration_ms} ms)")
            if result.semantic:
                for probe in result.semantic:
                    print(
                        "       semantic:",
                        f"caps={probe.capabilities}",
                        f"flow={probe.flow_signal}",
                        f"error={probe.error}",
                    )
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
        print(f"Total cases: {len(report.results)}")
        print(f"Report: {path}")
        print(f"Database writes rolled back: {'no' if args.keep_data else 'yes'}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
