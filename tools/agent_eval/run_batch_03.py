from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.handoff_request import HandoffRequest
from app.models.message import Message
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.service import Service
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.agent_v2.state_persistence import load_active_task
from app.services.handoffs import (
    add_staff_reply,
    claim_handoff,
    create_handoff,
    get_active_handoff,
    resolve_handoff,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    acquire_eval_advisory_lock,
    active_branch_id,
    assert_demo_only,
    batch_token_summary,
    booking_context,
    default_evaluation,
    jsonable,
    local_slot,
    money,
    send_turn,
    service_by_slug,
    state_snapshot,
)
from tools.agent_eval.run_batch_01 import (
    context_with_two_doctors,
    created_appointments,
    doctor_name,
    quiet_patient,
)
from tools.agent_eval.run_batch_02 import (
    _active_task,
    _apply_cost,
    _compatible_doctor_id,
    _ensure_batch2_catalog_fixtures,
    _history_pairs,
    _seed_historical_appointment,
    _seed_history_conversation,
    _structured_ops,
    laser_context,
)
from tools.agent_eval.run_pulse_domain import _active_patient

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
BATCH_NUMBER = 3
SCENARIO_VERSION = "batch3-v1"
BATCH3_FIXTURE_VERSION = "batch3-demo-fixtures-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--output-dir", default="backend/eval_results")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA") or "unknown")
    parser.add_argument("--batch3-base-sha", required=True)
    parser.add_argument("--input-price-per-million", type=float, required=True)
    parser.add_argument("--cached-input-price-per-million", type=float, required=True)
    parser.add_argument("--output-price-per-million", type=float, required=True)
    parser.add_argument("--cache-write-multiplier", type=float, default=1.0)
    parser.add_argument("--pricing-source", required=True)
    return parser.parse_args()


def require_explicit_demo_eval() -> None:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError(
            "Set TIA_AGENT_EVAL_CONFIRM_DEMO=1 to run the live Demo evaluation."
        )


def _package_usage_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> list[dict[str, Any]]:
    package_ids = list(
        db.scalars(
            select(PatientPackage.id).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
            )
        )
    )
    if not package_ids:
        return []
    rows = list(
        db.scalars(
            select(PackageUsage)
            .where(
                PackageUsage.workspace_id == workspace.id,
                PackageUsage.patient_package_id.in_(package_ids),
            )
            .order_by(PackageUsage.created_at, PackageUsage.id)
        )
    )
    return [
        {
            "id": str(row.id),
            "patient_package_id": str(row.patient_package_id),
            "appointment_id": str(row.appointment_id),
            "sessions_used": int(row.sessions_used),
            "status": row.status,
        }
        for row in rows
    ]


def _handoff_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(HandoffRequest)
            .where(
                HandoffRequest.workspace_id == workspace.id,
                HandoffRequest.patient_id == patient.id,
            )
            .order_by(HandoffRequest.created_at, HandoffRequest.id)
        )
    )
    return [
        {
            "id": str(row.id),
            "conversation_id": str(row.conversation_id),
            "status": row.status,
            "category": row.category,
            "priority": row.priority,
            "source": row.source,
            "assigned_user_id": str(row.assigned_user_id)
            if row.assigned_user_id
            else None,
        }
        for row in rows
    ]


def extended_state_snapshot(
    db: Session,
    workspace: Workspace,
    patient: Patient,
) -> dict[str, Any]:
    return {
        **state_snapshot(db, workspace, patient),
        "package_usages": _package_usage_snapshot(db, workspace, patient),
        "handoffs": _handoff_snapshot(db, workspace, patient),
    }


def db_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    collections = (
        "appointments",
        "packages",
        "package_usages",
        "pulse_packs",
        "payments",
        "pulse_usages",
        "pulse_settlements",
        "handoffs",
    )
    output: dict[str, Any] = {}
    for key in collections:
        left = before.get(key) or []
        right = after.get(key) or []
        left_by_id = {str(row["id"]): row for row in left if row.get("id")}
        right_by_id = {str(row["id"]): row for row in right if row.get("id")}
        output[key] = {
            "created": sorted(set(right_by_id) - set(left_by_id)),
            "removed": sorted(set(left_by_id) - set(right_by_id)),
            "changed": sorted(
                row_id
                for row_id in set(left_by_id) & set(right_by_id)
                if left_by_id[row_id] != right_by_id[row_id]
            ),
        }

    before_balances = {
        str(row.get("device_key")): int(row.get("pulses_remaining") or 0)
        for row in before.get("pulse_balances") or []
    }
    after_balances = {
        str(row.get("device_key")): int(row.get("pulses_remaining") or 0)
        for row in after.get("pulse_balances") or []
    }
    output["pulse_balance_delta"] = {
        key: after_balances.get(key, 0) - before_balances.get(key, 0)
        for key in sorted(set(before_balances) | set(after_balances))
        if after_balances.get(key, 0) != before_balances.get(key, 0)
    }
    return output


def ownership_snapshot(
    db: Session,
    workspace: Workspace,
    conversation_id: UUID | None,
) -> dict[str, Any] | None:
    if conversation_id is None:
        return None
    row = db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == workspace.id,
            Conversation.id == conversation_id,
        )
    )
    if row is None:
        return None
    handoff = get_active_handoff(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
    )
    return {
        "conversation_id": str(row.id),
        "owner_type": row.owner_type,
        "status": row.status,
        "assigned_user_id": str(row.assigned_user_id) if row.assigned_user_id else None,
        "ownership_epoch": (
            row.ownership_changed_at.isoformat() if row.ownership_changed_at else None
        ),
        "active_handoff_id": str(handoff.id) if handoff else None,
        "active_handoff_status": handoff.status if handoff else None,
    }


def _active_staff_user(db: Session, workspace: Workspace) -> User:
    row = db.scalar(
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.is_active.is_(True),
            User.is_active.is_(True),
        )
        .order_by(User.created_at.asc())
        .limit(1)
    )
    if row is None:
        raise RuntimeError("EVAL_INFRA_ERROR: no active workspace staff user")
    return row


def _claim_reply_and_handback(
    db: Session,
    workspace: Workspace,
    conversation_id: UUID,
    *,
    staff_text: str,
) -> dict[str, Any]:
    staff = _active_staff_user(db, workspace)
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise RuntimeError("EVAL_INFRA_ERROR: conversation missing during handoff")
    handoff = get_active_handoff(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation_id,
    )
    if handoff is None:
        return {
            "missing_handoff": True,
            "before_staff": ownership_snapshot(db, workspace, conversation_id),
            "during_staff": None,
            "after_handback": ownership_snapshot(db, workspace, conversation_id),
            "staff_message_id": None,
            "staff_message": staff_text,
        }
    before = ownership_snapshot(db, workspace, conversation_id)
    claim_handoff(
        db,
        handoff=handoff,
        conversation=conversation,
        user=staff,
        commit=False,
    )
    staff_message = add_staff_reply(
        db,
        handoff=handoff,
        conversation=conversation,
        user=staff,
        content=staff_text,
        commit=False,
    )
    db.flush()
    staff_message.delivery_status = "sent"
    db.flush()
    claimed = ownership_snapshot(db, workspace, conversation_id)
    resolve_handoff(
        db,
        handoff=handoff,
        conversation=conversation,
        actor_user=staff,
        resolution_note="Batch 3 eval handback",
        conversation_status_after="open",
    )
    after = ownership_snapshot(db, workspace, conversation_id)
    return {
        "before_staff": before,
        "during_staff": claimed,
        "after_handback": after,
        "staff_message_id": str(staff_message.id),
        "staff_message": staff_text,
    }


def _manual_staff_takeover(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    conversation_id: UUID,
    *,
    staff_text: str | None = None,
) -> tuple[HandoffRequest, User, dict[str, Any]]:
    staff = _active_staff_user(db, workspace)
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise RuntimeError("EVAL_INFRA_ERROR: conversation missing")
    before = ownership_snapshot(db, workspace, conversation_id)
    handoff = create_handoff(
        db,
        workspace_id=workspace.id,
        conversation=conversation,
        patient=patient,
        reason="batch3_manual_staff_takeover",
        category="other",
        priority="normal",
        source="staff",
        created_by_user_id=staff.id,
        commit=False,
    )
    claim_handoff(
        db,
        handoff=handoff,
        conversation=conversation,
        user=staff,
        commit=False,
    )
    message_id = None
    if staff_text:
        message = add_staff_reply(
            db,
            handoff=handoff,
            conversation=conversation,
            user=staff,
            content=staff_text,
            commit=False,
        )
        message.delivery_status = "sent"
        message_id = str(message.id)
    db.flush()
    return (
        handoff,
        staff,
        {
            "before_takeover": before,
            "human_owned": ownership_snapshot(db, workspace, conversation_id),
            "staff_message_id": message_id,
            "staff_message": staff_text,
        },
    )


def _handback(
    db: Session,
    workspace: Workspace,
    conversation_id: UUID,
    handoff: HandoffRequest,
    staff: User,
) -> dict[str, Any]:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise RuntimeError("EVAL_INFRA_ERROR: conversation missing")
    resolve_handoff(
        db,
        handoff=handoff,
        conversation=conversation,
        actor_user=staff,
        resolution_note="Batch 3 eval handback",
        conversation_status_after="open",
    )
    return ownership_snapshot(db, workspace, conversation_id) or {}


def _seed_package_balance(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    service: Service,
    *,
    remaining: int,
    name: str,
    expires_at: date | None = None,
    purchased_days_ago: int = 60,
) -> PatientPackage:
    package = PatientPackage(
        workspace_id=workspace.id,
        patient_id=patient.id,
        service_id=service.id,
        name=name,
        sessions_purchased=max(3, remaining, 1),
        opening_sessions_remaining=max(0, remaining),
        sessions_total_known=True,
        sale_price_minor=max(service.price_minor * max(3, remaining, 1), 0),
        standalone_session_price_minor_at_purchase=service.price_minor,
        currency="EGP",
        purchased_at=datetime.now(UTC) - timedelta(days=purchased_days_ago),
        expires_at=expires_at,
        status="active",
        source="staff",
        idempotency_key=f"batch3:{patient.id}:{service.id}:{name}",
    )
    db.add(package)
    db.flush()
    return package


def _future_slot(
    db: Session,
    workspace: Workspace,
    *,
    service_id: str,
    doctor_id: str,
    device_key: str | None = None,
    not_on_date: date | None = None,
    after_date: date | None = None,
    exclude_appointment_id: str | None = None,
):
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    today = datetime.now(UTC).date()
    for offset in range(1, 70):
        day = today + timedelta(days=offset)
        if after_date is not None and day <= after_date:
            continue
        if not_on_date is not None and day == not_on_date:
            continue
        available = adapter.get_availability(
            AvailabilityRequest(
                branch_id=branch_id,
                service_id=service_id,
                booking_date=day,
                doctor_id=doctor_id,
                exclude_appointment_id=exclude_appointment_id,
                laser_device_key=device_key,
            )
        )
        if available.slots:
            return available, available.slots[0]
    raise RuntimeError("EVAL_INFRA_ERROR: no future slot for requested fixture")


def _seed_future_appointment(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    *,
    service: Service,
    doctor_id: UUID,
    slot,
    device_key: str | None = None,
) -> Appointment:
    row = Appointment(
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=UUID(str(slot.branch_id)),
        doctor_id=doctor_id,
        service_id=service.id,
        status="confirmed",
        source="staff",
        start_at=slot.start_at,
        end_at=slot.end_at,
        busy_start_at=slot.start_at,
        busy_end_at=slot.end_at,
        duration_minutes=slot.duration_minutes,
        price_minor=slot.price_minor,
        currency=slot.currency,
        payment_status="unpaid",
        payment_method="unknown",
        billing_context="standard",
        laser_device_key=device_key,
        laser_device_name=slot.laser_device_name if device_key else None,
        confirmed_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def _run_messages(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    scenario_id: str,
    messages: list[str],
    *,
    conversation_id: UUID | None = None,
):
    turns = []
    current = conversation_id
    for index, message in enumerate(messages, start=1):
        response, turn = send_turn(
            db,
            workspace,
            patient,
            scenario_id,
            index,
            message,
            current,
        )
        current = response.conversation_id
        turns.append(turn)
    return turns, current


def _messages_for_conversation(
    db: Session,
    workspace: Workspace,
    conversation_id: UUID,
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == workspace.id,
                Message.conversation_id == conversation_id,
            )
            .order_by(Message.created_at, Message.id)
        )
    )
    return [
        {
            "id": str(row.id),
            "sender_type": row.sender_type,
            "direction": row.direction,
            "content": row.content,
            "delivery_status": row.delivery_status,
            "in_reply_to_message_id": (
                str(row.in_reply_to_message_id) if row.in_reply_to_message_id else None
            ),
        }
        for row in rows
    ]


def _observed(turns) -> dict[str, Any]:
    return {
        "turns": len(turns),
        "verified_reads": [turn.verified_reads for turn in turns],
        "writes": [
            {
                "attempted": turn.write_attempted,
                "result": turn.write_result,
                "actions": turn.actions,
            }
            for turn in turns
        ],
        "replies": [turn.agent_response for turn in turns],
        "structured_operations": [_structured_ops(turn) for turn in turns],
        "active_tasks": [_active_task(turn) for turn in turns],
        "runtime_states": [turn.runtime_state for turn in turns],
    }


def make_result(
    *,
    scenario_id: str,
    category: str,
    purpose: str,
    turns,
    before: dict[str, Any],
    after: dict[str, Any],
    verification: dict[str, Any],
    deterministic_ok: bool,
    expected: str,
    issue_severity: str = "P1",
    issue_title: str = "Deterministic contract mismatch",
    issue_detail: str = "",
    handoff_ok: bool | None = None,
) -> ScenarioResult:
    issues = []
    if not deterministic_ok:
        issues.append(
            {
                "severity": issue_severity,
                "title": issue_title,
                "detail": issue_detail,
            }
        )
    verification = {
        **verification,
        "db_delta": db_delta(before, after),
        "handoffs": [turn.handoff_state for turn in turns if turn.handoff_state],
    }
    return ScenarioResult(
        id=scenario_id,
        category=category,
        purpose=purpose,
        turns=list(turns),
        state_before=before,
        state_after=after,
        db_verification=verification,
        evaluation=default_evaluation(
            db_ok=deterministic_ok,
            grounding_ok=deterministic_ok,
            continuity_ok=deterministic_ok,
            handoff_ok=handoff_ok,
        ),
        issues=issues,
        token_usage={
            key: sum(int(turn.token_usage.get(key, 0)) for turn in turns)
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "cache_write_tokens",
                "uncached_input_tokens",
                "total_tokens",
                "calls",
                "metadata_missing_calls",
            )
        },
        review={
            "status": "PENDING_MANUAL_REVIEW",
            "expected": expected,
            "observed": _observed(turns),
            "reviewer_notes": "",
            "severity": None,
            "root_cause": None,
        },
    )


def _appointment_by_id(
    snapshot: dict[str, Any],
    appointment_id: UUID | str,
) -> dict[str, Any]:
    target = str(appointment_id)
    return next(row for row in snapshot["appointments"] if row["id"] == target)


def _usage_for_package(
    snapshot: dict[str, Any],
    package_id: UUID | str,
) -> list[dict[str, Any]]:
    target = str(package_id)
    return [
        row for row in snapshot["package_usages"] if row["patient_package_id"] == target
    ]


def _pulse_safe(delta: dict[str, Any]) -> bool:
    return (
        not delta["pulse_balance_delta"]
        and not delta["pulse_usages"]["created"]
        and not delta["pulse_settlements"]["created"]
        and not delta["payments"]["created"]
    )


def case_01_handoff_during_active_booking(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    target_date, _ = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns = []

    response, turn = send_turn(
        db,
        workspace,
        patient,
        "b3_01_handoff_during_active_booking",
        1,
        f"عايزة أحجز {service['name']} على كانديلا يوم {target_date}",
        None,
    )
    turns.append(turn)
    conversation_id = response.conversation_id
    task_before_handoff = _active_task(turn)

    response, turn = send_turn(
        db,
        workspace,
        patient,
        "b3_01_handoff_during_active_booking",
        2,
        "طب وفاضل عليا كام فلوس؟",
        conversation_id,
    )
    turns.append(turn)
    human_owned = ownership_snapshot(db, workspace, conversation_id)
    no_booking_while_human = not created_appointments(
        before,
        extended_state_snapshot(db, workspace, patient),
    )
    staff_evidence = _claim_reply_and_handback(
        db,
        workspace,
        conversation_id,
        staff_text="هراجع الحساب معاكي من الريسبشن.",
    )

    response, turn = send_turn(
        db,
        workspace,
        patient,
        "b3_01_handoff_during_active_booking",
        3,
        "تمام نكمل الحجز",
        conversation_id,
    )
    turns.append(turn)
    after = extended_state_snapshot(db, workspace, patient)
    resumed_task = _active_task(turn)
    resumed_serialized = json.dumps(resumed_task or {}, default=str)
    ok = (
        human_owned is not None
        and human_owned["owner_type"] == "human"
        and no_booking_while_human
        and not staff_evidence.get("missing_handoff")
        and staff_evidence["after_handback"]["owner_type"] == "ai"
        and bool(resumed_task)
        and str(service["id"]) in resumed_serialized
    )
    return make_result(
        scenario_id="b3_01_handoff_during_active_booking",
        category="handoff_handback",
        purpose=(
            "Financial handoff must preserve a safe active booking and resume only "
            "after handback."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "task_before_handoff": task_before_handoff,
            "human_owned": human_owned,
            "staff_evidence": staff_evidence,
            "resumed_task": resumed_task,
            "no_booking_while_human": no_booking_while_human,
        },
        deterministic_ok=ok,
        expected=(
            "Human owns the financial interval; no booking write occurs there; "
            "handback resumes the verified laser/Candela/date task."
        ),
        issue_severity="P1",
        issue_title="Handoff/handback lost booking state or violated ownership",
        issue_detail=(
            "A human-owned interval must suppress Agent action and legitimate "
            "handback must resume the safe booking state."
        ),
        handoff_ok=ok,
    )


def case_02_staff_takeover_then_handback(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, _, _, available = booking_context(
        db,
        workspace,
        service_slug="hydrafacial",
    )
    date_text, time_text = local_slot(available, available.slots[0])
    before = extended_state_snapshot(db, workspace, patient)
    turns = []

    response, turn = send_turn(
        db,
        workspace,
        patient,
        "b3_02_staff_takeover_then_handback",
        1,
        f"عايزة أحجز {service['name']} يوم {date_text}",
        None,
    )
    turns.append(turn)
    conversation_id = response.conversation_id
    handoff, staff, ownership = _manual_staff_takeover(
        db,
        workspace,
        patient,
        conversation_id,
        staff_text="تمام يا فندم، أنا مع حضرتك من الريسبشن.",
    )

    _, human_turn = send_turn(
        db,
        workspace,
        patient,
        "b3_02_staff_takeover_then_handback",
        2,
        f"خليه الساعة {time_text}",
        conversation_id,
    )
    turns.append(human_turn)
    silent = (
        human_turn.agent_response is None
        and human_turn.token_usage.get("calls", 0) == 0
        and not human_turn.write_attempted
    )
    handback = _handback(db, workspace, conversation_id, handoff, staff)
    _, resumed = send_turn(
        db,
        workspace,
        patient,
        "b3_02_staff_takeover_then_handback",
        3,
        "تمام كملي",
        conversation_id,
    )
    turns.append(resumed)
    messages = _messages_for_conversation(db, workspace, conversation_id)
    after = extended_state_snapshot(db, workspace, patient)
    duplicated_staff_text = any(
        row["content"] == ownership["staff_message"] and row["sender_type"] == "ai"
        for row in messages
    )
    ok = silent and handback.get("owner_type") == "ai" and not duplicated_staff_text
    return make_result(
        scenario_id="b3_02_staff_takeover_then_handback",
        category="handoff_handback",
        purpose=(
            "A real staff takeover must suppress AI, then handback must not replay "
            "staff-owned work."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "ownership": ownership,
            "agent_silent_while_human": silent,
            "handback": handback,
            "messages": messages,
            "duplicate_staff_reply": duplicated_staff_text,
        },
        deterministic_ok=ok,
        expected=(
            "No Agent response/write while Reception owns the chat; after handback "
            "only the new customer turn is handled and the staff reply is not duplicated."
        ),
        issue_severity="P0" if human_turn.write_attempted else "P1",
        issue_title="Ownership epoch allowed stale or duplicate Agent handling",
        issue_detail=(
            "Human-owned inbound must not trigger an Agent response/write and old "
            "staff handling must not be replayed."
        ),
        handoff_ok=ok,
    )


def case_03_financial_handoff_then_new_booking(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = _active_patient(db, workspace)
    _, service, doctor, _, _, available = booking_context(
        db,
        workspace,
        service_slug="hydrafacial",
    )
    date_text, time_text = local_slot(available, available.slots[0])
    before = extended_state_snapshot(db, workspace, patient)
    turns = []

    response, first = send_turn(
        db,
        workspace,
        patient,
        "b3_03_financial_handoff_then_new_booking",
        1,
        "فاضل عليا كام فلوس؟",
        None,
    )
    turns.append(first)
    conversation_id = response.conversation_id
    human = ownership_snapshot(db, workspace, conversation_id)
    staff_evidence = _claim_reply_and_handback(
        db,
        workspace,
        conversation_id,
        staff_text="الحسابات بنراجعها من الريسبشن.",
    )
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b3_03_financial_handoff_then_new_booking",
        2,
        (
            f"تمام عايزة أحجز {service['name']} يوم {date_text} الساعة "
            f"{time_text} مع {doctor_name(doctor)}"
        ),
        conversation_id,
    )
    turns.append(second)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    delta = db_delta(before, after)
    ok = (
        human is not None
        and human["owner_type"] == "human"
        and not staff_evidence.get("missing_handoff")
        and staff_evidence["after_handback"]["owner_type"] == "ai"
        and len(created) == 1
        and created[0]["service_id"] == str(service["id"])
        and _pulse_safe(delta)
    )
    return make_result(
        scenario_id="b3_03_financial_handoff_then_new_booking",
        category="handoff_handback",
        purpose="A resolved financial handoff must not poison a later unrelated booking.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "human_owned": human,
            "staff_evidence": staff_evidence,
            "created": created,
            "financial_safe": _pulse_safe(delta),
        },
        deterministic_ok=ok,
        expected=(
            "Financial question hands off; after handback an explicit Hydrafacial "
            "booking proceeds cleanly with no financial mutation."
        ),
        issue_severity="P0" if not _pulse_safe(delta) else "P1",
        issue_title="Financial handoff leaked into later booking",
        issue_detail=(
            "Resolved financial ownership must not block or contaminate a new booking intent."
        ),
        handoff_ok=ok,
    )


def case_04_lifecycle_while_human_owns(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, _, doctor, _, _, available = booking_context(
        db,
        workspace,
        service_slug="hydrafacial",
    )
    service = service_by_slug(db, workspace, "hydrafacial")
    existing = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        slot=available.slots[0],
    )
    old_date, _ = local_slot(available, available.slots[0])
    new_available, new_slot = _future_slot(
        db,
        workspace,
        service_id=str(existing.service_id),
        doctor_id=str(existing.doctor_id),
        after_date=existing.start_at.astimezone(
            ZoneInfo(workspace.timezone or "UTC")
        ).date(),
        exclude_appointment_id=str(existing.id),
    )
    new_date, new_time = local_slot(new_available, new_slot)
    before = extended_state_snapshot(db, workspace, patient)

    response, starter = send_turn(
        db,
        workspace,
        patient,
        "b3_04_lifecycle_while_human_owns",
        1,
        "هاي",
        None,
    )
    conversation_id = response.conversation_id
    handoff, staff, ownership = _manual_staff_takeover(
        db,
        workspace,
        patient,
        conversation_id,
        staff_text="أنا من الريسبشن وهكمل معاكي.",
    )
    _, human_turn = send_turn(
        db,
        workspace,
        patient,
        "b3_04_lifecycle_while_human_owns",
        2,
        f"غيري ميعاد {old_date}",
        conversation_id,
    )
    mid = extended_state_snapshot(db, workspace, patient)
    silent = (
        human_turn.agent_response is None
        and not human_turn.write_attempted
        and _appointment_by_id(mid, existing.id)["status"] == "confirmed"
    )
    handback = _handback(db, workspace, conversation_id, handoff, staff)
    _, agent_turn = send_turn(
        db,
        workspace,
        patient,
        "b3_04_lifecycle_while_human_owns",
        3,
        f"دلوقتي غيريه ليوم {new_date} الساعة {new_time}",
        conversation_id,
    )
    after = extended_state_snapshot(db, workspace, patient)
    original = _appointment_by_id(after, existing.id)
    replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == str(existing.id)
    ]
    ok = (
        silent
        and handback.get("owner_type") == "ai"
        and original["status"] == "rescheduled"
        and len(replacements) == 1
    )
    return make_result(
        scenario_id="b3_04_lifecycle_while_human_owns",
        category="handoff_handback",
        purpose=(
            "Lifecycle writes are forbidden while human owns the conversation and "
            "resume only after handback."
        ),
        turns=[starter, human_turn, agent_turn],
        before=before,
        after=after,
        verification={
            "ownership": ownership,
            "silent_while_human": silent,
            "handback": handback,
            "original": original,
            "replacements": replacements,
        },
        deterministic_ok=ok,
        expected=(
            "The first reschedule request is suppressed under human ownership; after "
            "handback the explicit new request reschedules exactly once."
        ),
        issue_severity="P0" if human_turn.write_attempted else "P1",
        issue_title="Lifecycle action crossed human ownership boundary",
        issue_detail="No appointment write may occur while Reception owns the conversation.",
        handoff_ok=ok,
    )


def _seed_two_appointments(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    *,
    same_service: bool,
    laser: bool = False,
):
    if laser:
        _, _, doctor, available1, slot1 = laser_context(
            db,
            workspace,
            service_slug="laser-hair-removal-underarm",
            device_key="candela_gentle",
        )
        service1 = service_by_slug(db, workspace, "laser-hair-removal-underarm")
        doctor1 = UUID(str(doctor["id"]))
        appt1 = _seed_future_appointment(
            db,
            workspace,
            patient,
            service=service1,
            doctor_id=doctor1,
            slot=slot1,
            device_key="candela_gentle",
        )
        day1 = slot1.start_at.astimezone(ZoneInfo(available1.timezone)).date()
        available2, slot2 = _future_slot(
            db,
            workspace,
            service_id=str(service1.id),
            doctor_id=str(doctor1),
            device_key="candela_gentle",
            after_date=day1,
        )
        appt2 = _seed_future_appointment(
            db,
            workspace,
            patient,
            service=service1,
            doctor_id=doctor1,
            slot=slot2,
            device_key="candela_gentle",
        )
        return (appt1, available1, slot1), (appt2, available2, slot2)

    _, _, doctor1_row, _, _, available1 = booking_context(
        db,
        workspace,
        service_slug="prp-skin",
    )
    service1 = service_by_slug(db, workspace, "prp-skin")
    slot1 = available1.slots[0]
    appt1 = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service1,
        doctor_id=UUID(str(doctor1_row["id"])),
        slot=slot1,
    )
    day1 = slot1.start_at.astimezone(ZoneInfo(available1.timezone)).date()
    if same_service:
        available2, slot2 = _future_slot(
            db,
            workspace,
            service_id=str(service1.id),
            doctor_id=str(doctor1_row["id"]),
            after_date=day1,
        )
        appt2 = _seed_future_appointment(
            db,
            workspace,
            patient,
            service=service1,
            doctor_id=UUID(str(doctor1_row["id"])),
            slot=slot2,
        )
        return (appt1, available1, slot1), (appt2, available2, slot2)

    _, _, doctor2_row, _, _, available2 = booking_context(
        db,
        workspace,
        service_slug="hydrafacial",
    )
    service2 = service_by_slug(db, workspace, "hydrafacial")
    slot2 = available2.slots[0]
    if slot2.start_at.astimezone(ZoneInfo(available2.timezone)).date() == day1:
        available2, slot2 = _future_slot(
            db,
            workspace,
            service_id=str(service2.id),
            doctor_id=str(doctor2_row["id"]),
            after_date=day1,
        )
    appt2 = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service2,
        doctor_id=UUID(str(doctor2_row["id"])),
        slot=slot2,
    )
    return (appt1, available1, slot1), (appt2, available2, slot2)


def case_05_two_appointments_explicit_date(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    (first, _, _), (second, second_av, second_slot) = _seed_two_appointments(
        db,
        workspace,
        patient,
        same_service=False,
    )
    selected_date, _ = local_slot(second_av, second_slot)
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=str(second.service_id),
        doctor_id=str(second.doctor_id),
        after_date=second.start_at.astimezone(
            ZoneInfo(workspace.timezone or "UTC")
        ).date(),
        exclude_appointment_id=str(second.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_05_two_appointments_explicit_date",
        [
            f"غيري ميعاد يوم {selected_date} وخليه يوم {target_date} الساعة {target_time}"
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    first_after = _appointment_by_id(after, first.id)
    second_after = _appointment_by_id(after, second.id)
    replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == str(second.id)
    ]
    ok = (
        first_after["status"] == "confirmed"
        and second_after["status"] == "rescheduled"
        and len(replacements) == 1
    )
    return make_result(
        scenario_id="b3_05_two_appointments_explicit_date",
        category="multi_appointment",
        purpose=(
            "An explicit date must select the matching appointment among two upcoming "
            "appointments."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "selected_appointment_id": str(second.id),
            "untouched_appointment_id": str(first.id),
            "replacements": replacements,
        },
        deterministic_ok=ok,
        expected=(
            "Only the appointment on the stated date is rescheduled; the other "
            "appointment is untouched."
        ),
        issue_title="Explicit date resolved the wrong appointment",
        issue_detail=(
            "Multi-appointment resolution must use canonical appointment candidates "
            "and date constraints."
        ),
    )


def case_06_two_same_service_explicit_date(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    (first, _, _), (second, second_av, second_slot) = _seed_two_appointments(
        db,
        workspace,
        patient,
        same_service=True,
    )
    selected_date, _ = local_slot(second_av, second_slot)
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=str(second.service_id),
        doctor_id=str(second.doctor_id),
        after_date=second.start_at.astimezone(
            ZoneInfo(workspace.timezone or "UTC")
        ).date(),
        exclude_appointment_id=str(second.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_06_two_same_service_explicit_date",
        [
            f"عايزة أغير ميعاد اللي يوم {selected_date} لـ {target_date} الساعة {target_time}"
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == str(second.id)
    ]
    ok = (
        _appointment_by_id(after, first.id)["status"] == "confirmed"
        and _appointment_by_id(after, second.id)["status"] == "rescheduled"
        and len(replacements) == 1
    )
    return make_result(
        scenario_id="b3_06_two_same_service_explicit_date",
        category="multi_appointment",
        purpose=(
            "Date disambiguation must work even when two future appointments have "
            "the same service."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "target_id": str(second.id),
            "other_id": str(first.id),
            "replacements": replacements,
        },
        deterministic_ok=ok,
        expected="Only the same-service appointment on the explicit date is rescheduled.",
        issue_title="Same-service appointment disambiguation selected the wrong row",
        issue_detail="The explicit appointment date must dominate service-only ambiguity.",
    )


def case_07_real_appointment_ambiguity(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    (first, _, _), (second, _, _) = _seed_two_appointments(
        db,
        workspace,
        patient,
        same_service=True,
        laser=True,
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_07_real_appointment_ambiguity",
        ["عايزة أغير ميعاد الليزر"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    no_write = (
        before["appointments"] == after["appointments"]
        and not turns[-1].write_attempted
    )
    ok = no_write and bool(turns[-1].agent_response)
    return make_result(
        scenario_id="b3_07_real_appointment_ambiguity",
        category="multi_appointment",
        purpose="A genuinely ambiguous lifecycle request must clarify instead of guessing.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "candidate_ids": [str(first.id), str(second.id)],
            "no_write": no_write,
            "structured_operations": _structured_ops(turns[-1]),
        },
        deterministic_ok=ok,
        expected="Clarification with zero appointment write because both laser appointments match.",
        issue_severity="P1" if not no_write else "P2",
        issue_title="Ambiguous appointment request guessed instead of clarifying",
        issue_detail=(
            "Two matching actionable appointments require verified candidate "
            "disambiguation before any write."
        ),
    )


def case_08_candidate_selection_followup(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    (first, _, _), (second, _, _) = _seed_two_appointments(
        db,
        workspace,
        patient,
        same_service=True,
        laser=True,
    )
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=str(second.service_id),
        doctor_id=str(second.doctor_id),
        device_key="candela_gentle",
        after_date=second.start_at.astimezone(
            ZoneInfo(workspace.timezone or "UTC")
        ).date(),
        exclude_appointment_id=str(second.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_08_candidate_selection_followup",
        [
            "عايزة أغير ميعاد الليزر",
            "التاني",
            f"خليه يوم {target_date} الساعة {target_time}",
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == str(second.id)
    ]
    wrong_replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == str(first.id)
    ]
    ok = (
        len(replacements) == 1
        and not wrong_replacements
        and _appointment_by_id(after, first.id)["status"] == "confirmed"
    )
    return make_result(
        scenario_id="b3_08_candidate_selection_followup",
        category="multi_appointment",
        purpose=(
            "A short ordinal follow-up must resolve against the verified appointment "
            "candidate set."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "first_candidate_id": str(first.id),
            "second_candidate_id": str(second.id),
            "replacements": replacements,
            "wrong_replacements": wrong_replacements,
        },
        deterministic_ok=ok,
        expected=(
            "'التاني' binds to the second verified candidate, and only that "
            "appointment is rescheduled."
        ),
        issue_title="Ordinal follow-up selected stale or wrong appointment",
        issue_detail=(
            "Candidate selection must bind to the current verified appointment set, "
            "not history or guesswork."
        ),
    )


def case_09_cancel_one_of_multiple(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    (first, _, _), (second, _, _) = _seed_two_appointments(
        db,
        workspace,
        patient,
        same_service=True,
        laser=True,
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_09_cancel_one_of_multiple",
        ["عايزة ألغي ميعاد الليزر", "التاني", "أيوه الغيه"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    first_after = _appointment_by_id(after, first.id)
    second_after = _appointment_by_id(after, second.id)
    cancelled = [row for row in after["appointments"] if row["status"] == "cancelled"]
    ok = (
        first_after["status"] == "confirmed"
        and second_after["status"] == "cancelled"
        and sum(row["id"] == str(second.id) for row in cancelled) == 1
    )
    return make_result(
        scenario_id="b3_09_cancel_one_of_multiple",
        category="multi_appointment",
        purpose=(
            "After verified disambiguation, cancellation must affect the selected "
            "appointment exactly once."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "selected_id": str(second.id),
            "other_id": str(first.id),
            "cancelled": cancelled,
        },
        deterministic_ok=ok,
        expected=(
            "Only the selected second laser appointment is cancelled; the other "
            "remains confirmed."
        ),
        issue_severity="P0" if first_after["status"] == "cancelled" else "P1",
        issue_title="Cancellation affected the wrong appointment",
        issue_detail=(
            "Verified candidate selection must be preserved through the lifecycle write."
        ),
    )


def _package_booking_context(db: Session, workspace: Workspace):
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "prp-skin")
    _, _, doctor, _, _, available = booking_context(
        db,
        workspace,
        service_slug="prp-skin",
    )
    slot = available.slots[0]
    date_text, time_text = local_slot(available, slot)
    return patient, service, doctor, available, slot, date_text, time_text


def case_10_one_session_remaining(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(
        db,
        workspace,
    )
    package = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=1,
        name="Batch3 one remaining",
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_10_one_session_remaining",
        [
            (
                f"احجزيلي {service.name} من الباكدج مع {doctor_name(doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            )
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    usages = _usage_for_package(after, package.id)
    package_after = next(
        row for row in after["packages"] if row["id"] == str(package.id)
    )
    ok = (
        len(created) == 1
        and created[0]["patient_package_id"] == str(package.id)
        and created[0]["billing_context"] == "package_prepaid"
        and package_after["remaining"] == 0
        and len(usages) == 1
        and usages[0]["status"] == "reserved"
    )
    return make_result(
        scenario_id="b3_10_one_session_remaining",
        category="package_lifecycle",
        purpose="The last eligible package session must be reserved exactly once.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "package_id": str(package.id),
            "created": created,
            "usages": usages,
            "package_after": package_after,
        },
        deterministic_ok=ok,
        expected=(
            "One package-backed appointment, one reserved usage, remaining sessions "
            "1→0, no double decrement."
        ),
        issue_title="Last package session was not reserved exactly once",
        issue_detail=(
            "Package lifecycle must create one reservation and one package-backed "
            "appointment only."
        ),
    )


def case_11_exhausted_package(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(
        db,
        workspace,
    )
    package = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=0,
        name="Batch3 exhausted",
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_11_exhausted_package",
        [
            (
                f"احجزيلي {service.name} من الباكدج يوم {date_text} الساعة "
                f"{time_text} مع {doctor_name(doctor)}"
            )
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    wrong = [row for row in created if row["patient_package_id"] == str(package.id)]
    ok = not created and not wrong and not _usage_for_package(after, package.id)
    return make_result(
        scenario_id="b3_11_exhausted_package",
        category="package_lifecycle",
        purpose="An exhausted package must never back a new booking.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "package_id": str(package.id),
            "created": created,
            "package_usages": _usage_for_package(after, package.id),
        },
        deterministic_ok=ok,
        expected=(
            "No package-backed or fallback standalone booking is silently created "
            "from an explicit exhausted-package request."
        ),
        issue_severity="P1",
        issue_title="Exhausted package was used or silently bypassed",
        issue_detail=(
            "Explicit use_existing requires a usable canonical package; exhausted "
            "entitlement must not be consumed."
        ),
    )


def case_12_expired_package(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(
        db,
        workspace,
    )
    package = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Batch3 expired",
        expires_at=datetime.now(UTC).date() - timedelta(days=1),
        purchased_days_ago=90,
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_12_expired_package",
        [
            (
                f"احجزيلي {service.name} من الباكدج يوم {date_text} الساعة "
                f"{time_text} مع {doctor_name(doctor)}"
            )
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    wrong = [row for row in created if row["patient_package_id"] == str(package.id)]
    ok = not created and not wrong and not _usage_for_package(after, package.id)
    return make_result(
        scenario_id="b3_12_expired_package",
        category="package_lifecycle",
        purpose="Remaining sessions do not make an expired package eligible.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "package_id": str(package.id),
            "created": created,
            "package_usages": _usage_for_package(after, package.id),
        },
        deterministic_ok=ok,
        expected=(
            "Expired package is rejected from booking despite a positive recorded balance."
        ),
        issue_title="Expired package was treated as usable",
        issue_detail="Canonical effective_status and appointment date must gate package use.",
    )


def case_13_two_eligible_packages(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(
        db,
        workspace,
    )
    today = datetime.now(UTC).date()
    earlier = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Batch3 expires first",
        expires_at=today + timedelta(days=30),
        purchased_days_ago=80,
    )
    later = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Batch3 expires later",
        expires_at=today + timedelta(days=90),
        purchased_days_ago=20,
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_13_two_eligible_packages",
        [
            (
                f"احجزيلي {service.name} يوم {date_text} الساعة {time_text} مع "
                f"{doctor_name(doctor)} واستخدمي الباكدج"
            )
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = (
        len(created) == 1
        and created[0]["patient_package_id"] == str(earlier.id)
        and len(_usage_for_package(after, earlier.id)) == 1
        and not _usage_for_package(after, later.id)
    )
    safe_clarification = not created and not turns[-1].write_attempted
    severity = "P2" if safe_clarification else "P1"
    return make_result(
        scenario_id="b3_13_two_eligible_packages",
        category="package_lifecycle",
        purpose=(
            "Current canonical policy deterministically chooses the entitlement "
            "that expires first."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "expected_package_id": str(earlier.id),
            "other_package_id": str(later.id),
            "created": created,
            "expected_usages": _usage_for_package(after, earlier.id),
            "other_usages": _usage_for_package(after, later.id),
            "domain_policy": "earliest expiry, then oldest purchase, then id",
            "safe_clarification": safe_clarification,
        },
        deterministic_ok=ok,
        expected=(
            "Because the current domain is deterministic, the earlier-expiring "
            "eligible package is selected without arbitrary model choice."
        ),
        issue_severity=severity,
        issue_title="Multiple eligible packages did not follow canonical selection policy",
        issue_detail=(
            "Python domain policy—not model preference—must choose the "
            "earliest-expiring entitlement deterministically."
        ),
    )


def case_14_package_booking_then_cancel(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(
        db,
        workspace,
    )
    package = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Batch3 cancel lifecycle",
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b3_14_package_booking_then_cancel",
        [
            (
                f"احجزيلي {service.name} من الباكدج يوم {date_text} الساعة "
                f"{time_text} مع {doctor_name(doctor)}"
            )
        ],
    )
    mid = extended_state_snapshot(db, workspace, patient)
    created_mid = created_appointments(before, mid)
    _, cancel_turn = send_turn(
        db,
        workspace,
        patient,
        "b3_14_package_booking_then_cancel",
        2,
        "تمام، الغيه",
        conversation_id,
    )
    turns.append(cancel_turn)
    after = extended_state_snapshot(db, workspace, patient)
    package_after = next(
        row for row in after["packages"] if row["id"] == str(package.id)
    )
    usages = _usage_for_package(after, package.id)
    appointment = (
        _appointment_by_id(after, created_mid[0]["id"])
        if len(created_mid) == 1
        else None
    )
    ok = (
        len(created_mid) == 1
        and appointment is not None
        and appointment["status"] == "cancelled"
        and package_after["remaining"] == 2
        and len(usages) == 1
        and usages[0]["status"] == "released"
    )
    return make_result(
        scenario_id="b3_14_package_booking_then_cancel",
        category="package_lifecycle",
        purpose=(
            "Cancelling a package-backed appointment must restore the reservation "
            "exactly once."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "created_mid": created_mid,
            "appointment_after": appointment,
            "package_after": package_after,
            "usages": usages,
        },
        deterministic_ok=ok,
        expected=(
            "Appointment cancels, its single reservation becomes released, and "
            "package remaining returns to the original value exactly once."
        ),
        issue_title="Package cancellation lifecycle did not restore entitlement exactly once",
        issue_detail=(
            "Cancellation must release the reserved PackageUsage without double restoration."
        ),
    )


def case_15_package_booking_then_reschedule(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(
        db,
        workspace,
    )
    package = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Batch3 reschedule lifecycle",
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b3_15_package_booking_then_reschedule",
        [
            (
                f"احجزيلي {service.name} من الباكدج يوم {date_text} الساعة "
                f"{time_text} مع {doctor_name(doctor)}"
            )
        ],
    )
    mid = extended_state_snapshot(db, workspace, patient)
    created_mid = created_appointments(before, mid)
    if len(created_mid) != 1:
        return make_result(
            scenario_id="b3_15_package_booking_then_reschedule",
            category="package_lifecycle",
            purpose="Reschedule must preserve package identity and one reservation.",
            turns=turns,
            before=before,
            after=mid,
            verification={"created_mid": created_mid},
            deterministic_ok=False,
            expected="Initial package-backed booking succeeds before reschedule.",
            issue_title="Package reschedule setup booking did not complete",
            issue_detail=(
                "Cannot verify transfer because the initial canonical package booking failed."
            ),
        )

    current = created_mid[0]
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=current["service_id"],
        doctor_id=current["doctor_id"],
        after_date=datetime.fromisoformat(current["start_at"])
        .astimezone(ZoneInfo(workspace.timezone or "UTC"))
        .date(),
        exclude_appointment_id=current["id"],
    )
    target_date, target_time = local_slot(target_av, target_slot)
    _, move_turn = send_turn(
        db,
        workspace,
        patient,
        "b3_15_package_booking_then_reschedule",
        2,
        f"ممكن نغيره ليوم {target_date} الساعة {target_time}؟",
        conversation_id,
    )
    turns.append(move_turn)
    after = extended_state_snapshot(db, workspace, patient)
    original = _appointment_by_id(after, current["id"])
    replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == current["id"]
    ]
    usages = _usage_for_package(after, package.id)
    ok = (
        original["status"] == "rescheduled"
        and len(replacements) == 1
        and replacements[0]["patient_package_id"] == str(package.id)
        and replacements[0]["billing_context"] == "package_prepaid"
        and len(usages) == 1
        and usages[0]["status"] == "reserved"
        and usages[0]["appointment_id"] == replacements[0]["id"]
    )
    return make_result(
        scenario_id="b3_15_package_booking_then_reschedule",
        category="package_lifecycle",
        purpose=(
            "Reschedule must transfer one package reservation without converting "
            "billing context."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "original": original,
            "replacements": replacements,
            "usages": usages,
        },
        deterministic_ok=ok,
        expected=(
            "Original becomes rescheduled; replacement keeps package_prepaid and "
            "the same single reserved usage moves to it."
        ),
        issue_title="Package-backed reschedule lost package identity or duplicated usage",
        issue_detail=(
            "Reschedule must transfer—not release and recreate arbitrarily—the "
            "canonical PackageUsage."
        ),
    )


def case_16_old_cancelled_workflow_isolation(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    catalog = build_clinic_catalog(db, workspace)
    branch_id = UUID(active_branch_id(catalog))
    laser = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    cancelled = _seed_historical_appointment(
        db,
        workspace,
        patient,
        service=laser,
        doctor_id=_compatible_doctor_id(catalog, laser.id),
        branch_id=branch_id,
        days_ago=21,
        status="cancelled",
        device_key="prime_lase",
        device_name="Prime Lase",
    )
    conversation = _seed_history_conversation(
        db,
        workspace,
        patient,
        [
            ("عايزة أحجز ليزر", "تمام."),
            ("خليه الخميس", "تمام."),
            ("لا غيريه السبت", "حاضر."),
            ("خلاص الغيه", "تمام، اتلغى."),
        ],
    )
    _, hydra, doctor, _, _, available = booking_context(
        db,
        workspace,
        service_slug="hydrafacial",
    )
    date_text, time_text = local_slot(available, available.slots[0])
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_16_old_cancelled_workflow_isolation",
        [
            (
                f"عايزة أحجز {hydra['name']} يوم {date_text} الساعة {time_text} "
                f"مع {doctor_name(doctor)}"
            )
        ],
        conversation_id=conversation.id,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = (
        len(created) == 1
        and created[0]["service_id"] == str(hydra["id"])
        and _appointment_by_id(after, cancelled.id)["status"] == "cancelled"
    )
    return make_result(
        scenario_id="b3_16_old_cancelled_workflow_isolation",
        category="returning_customer_isolation",
        purpose=(
            "A completed old cancellation workflow must never revive into a new "
            "explicit booking."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "historical_cancelled_id": str(cancelled.id),
            "created": created,
        },
        deterministic_ok=ok,
        expected=(
            "Fresh Hydrafacial booking only; historical cancelled laser workflow "
            "remains cancelled and inert."
        ),
        issue_title="Old cancelled workflow leaked into a fresh booking",
        issue_detail=(
            "Explicit current booking intent must start from clean state instead "
            "of reviving historical lifecycle state."
        ),
    )


def case_17_old_doctor_preference_isolation(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, first, second = context_with_two_doctors(db, workspace)
    old_doctor, _, _ = first
    current_doctor, _, current_available = second
    slot = current_available.slots[0]
    date_text, time_text = local_slot(current_available, slot)
    conversation = _seed_history_conversation(
        db,
        workspace,
        patient,
        _history_pairs(stale_doctor=doctor_name(old_doctor)),
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_17_old_doctor_preference_isolation",
        [
            (
                f"المرة دي عايزة {service['name']} مع {doctor_name(current_doctor)} "
                f"يوم {date_text} الساعة {time_text}"
            )
        ],
        conversation_id=conversation.id,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["doctor_id"] == str(current_doctor["id"])
    return make_result(
        scenario_id="b3_17_old_doctor_preference_isolation",
        category="returning_customer_isolation",
        purpose=(
            "Historical doctor preference must not override an explicit current doctor."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "historical_doctor_id": str(old_doctor["id"]),
            "explicit_doctor_id": str(current_doctor["id"]),
            "created": created,
        },
        deterministic_ok=ok,
        expected=(
            "The explicitly requested current doctor wins completely over stale "
            "preference history."
        ),
        issue_title="Historical doctor preference overrode explicit current choice",
        issue_detail=(
            "Current explicit identity constraints must dominate conversational history."
        ),
    )


def case_18_old_device_isolation(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    conversation = _seed_history_conversation(
        db,
        workspace,
        patient,
        _history_pairs(stale_device="Prime Lase"),
    )
    _, service, doctor, available, slot = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    date_text, time_text = local_slot(available, slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_18_old_device_isolation",
        [
            (
                f"المرة دي عايزة {service['name']} على Candela مع "
                f"{doctor_name(doctor)} يوم {date_text} الساعة {time_text}"
            )
        ],
        conversation_id=conversation.id,
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) == 1 and created[0]["laser_device_key"] == "candela_gentle"
    return make_result(
        scenario_id="b3_18_old_device_isolation",
        category="returning_customer_isolation",
        purpose=(
            "Old laser device context must not override an explicit current device."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "historical_device": "prime_lase",
            "explicit_device": "candela_gentle",
            "created": created,
        },
        deterministic_ok=ok,
        expected=(
            "Candela is used for the current booking; old Prime Lase history does not leak."
        ),
        issue_title="Historical device leaked into explicit current booking",
        issue_detail="Explicit current device must override stale conversational device memory.",
    )


def case_19_price_package_booking(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient, service, doctor, _, _, date_text, time_text = _package_booking_context(
        db,
        workspace,
    )
    package = _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Batch3 multi intent package",
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_19_price_package_booking",
        [
            (
                f"{service.name} بكام دلوقتي؟ وهل الباكدج اللي عندي ينفع لها؟ "
                f"ولو ينفع احجزي يوم {date_text} الساعة {time_text} "
                f"مع {doctor_name(doctor)}"
            )
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    reply = (turns[-1].agent_response or "").replace(",", "")
    normalized_reply = reply.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    expected_price = money(service.price_minor).split(".", 1)[0]
    delta = db_delta(before, after)
    ok = (
        expected_price in normalized_reply
        and len(created) == 1
        and created[0]["patient_package_id"] == str(package.id)
        and _pulse_safe(delta)
    )
    return make_result(
        scenario_id="b3_19_price_package_booking",
        category="complex_multi_intent",
        purpose=(
            "Price, package eligibility, and booking must remain grounded without "
            "crossing Pulse/payment boundaries."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "canonical_price_minor": service.price_minor,
            "package_id": str(package.id),
            "created": created,
            "financial_safe": _pulse_safe(delta),
        },
        deterministic_ok=ok,
        expected=(
            "Canonical price and package eligibility are grounded, booking uses the "
            "session package, and no Pulse/payment mutation occurs."
        ),
        issue_severity="P0" if not _pulse_safe(delta) else "P1",
        issue_title="Price/package/booking compound intent mixed domains incorrectly",
        issue_detail=(
            "Session-package use may proceed, but Pulse settlement and payment "
            "remain Reception-owned."
        ),
    )


def case_20_side_query_during_reschedule(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, _, doctor, _, _, available = booking_context(
        db,
        workspace,
        service_slug="hydrafacial",
    )
    service = service_by_slug(db, workspace, "hydrafacial")
    existing = _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        slot=available.slots[0],
    )
    target_av, target_slot = _future_slot(
        db,
        workspace,
        service_id=str(existing.service_id),
        doctor_id=str(existing.doctor_id),
        after_date=existing.start_at.astimezone(
            ZoneInfo(workspace.timezone or "UTC")
        ).date(),
        exclude_appointment_id=str(existing.id),
    )
    target_date, target_time = local_slot(target_av, target_slot)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_20_side_query_during_reschedule",
        [
            "عايزة أغير ميعادي",
            "على فكرة الخدمة دي بكام دلوقتي؟",
            "تمام كملي تغيير الميعاد",
            f"خليه يوم {target_date} الساعة {target_time}",
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    replacements = [
        row
        for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") == str(existing.id)
    ]
    price_grounded = any(
        money(service.price_minor) in (turn.agent_response or "").replace(",", "")
        for turn in turns
    )
    ok = (
        price_grounded
        and _appointment_by_id(after, existing.id)["status"] == "rescheduled"
        and len(replacements) == 1
    )
    return make_result(
        scenario_id="b3_20_side_query_during_reschedule",
        category="complex_multi_intent",
        purpose=(
            "An informational price side-read must not reset the active reschedule target."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "existing_id": str(existing.id),
            "price_grounded": price_grounded,
            "replacements": replacements,
        },
        deterministic_ok=ok,
        expected=(
            "Price is answered from canonical data, then the original reschedule "
            "task resumes and moves the same appointment."
        ),
        issue_title="Informational side query destroyed lifecycle continuity",
        issue_detail=(
            "A read-only side intent must not replace the active reschedule target."
        ),
    )


def case_21_repeated_corrections(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, skin, skin_doctor, _, _, skin_available = booking_context(
        db,
        workspace,
        service_slug="prp-skin",
    )
    _, hair, hair_doctor, _, _, hair_available = booking_context(
        db,
        workspace,
        service_slug="prp-hair",
    )
    skin_date, _ = local_slot(skin_available, skin_available.slots[0])
    hair_date, hair_time = local_slot(hair_available, hair_available.slots[0])
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b3_21_repeated_corrections",
        [
            f"عايزة أحجز {skin['name']}",
            f"يوم {skin_date}",
            f"مع {doctor_name(skin_doctor)}",
            f"لا استنى خليها يوم {hair_date}",
            f"وفي الحقيقة مش البشرة، خليها {hair['name']}",
            f"ومع {doctor_name(hair_doctor)} الساعة {hair_time}",
        ],
    )
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = len(created) <= 1 and all(
        row["service_id"] == str(hair["id"])
        and row["doctor_id"] == str(hair_doctor["id"])
        for row in created
    )
    return make_result(
        scenario_id="b3_21_repeated_corrections",
        category="complex_multi_intent",
        purpose=(
            "Repeated service/date/doctor corrections must deterministically retain "
            "only the latest explicit values."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "superseded_service_id": str(skin["id"]),
            "final_service_id": str(hair["id"]),
            "final_doctor_id": str(hair_doctor["id"]),
            "final_date": hair_date,
            "final_time": hair_time,
            "created": created,
        },
        deterministic_ok=ok,
        expected=(
            "Final state reflects PRP hair + latest date + latest doctor only, with "
            "at most one write."
        ),
        issue_title="Repeated corrections leaked superseded booking dimensions",
        issue_detail=(
            "Latest explicit corrections must replace stale service/date/doctor "
            "state without duplicate writes."
        ),
    )


def case_22_abandon_then_different_booking(
    db: Session,
    workspace: Workspace,
) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, laser, _, laser_available, _ = laser_context(
        db,
        workspace,
        service_slug="laser-hair-removal-underarm",
        device_key="candela_gentle",
    )
    laser_date, _ = local_slot(laser_available, laser_available.slots[0])
    _, hydra, hydra_doctor, _, _, hydra_available = booking_context(
        db,
        workspace,
        service_slug="hydrafacial",
    )
    hydra_date, hydra_time = local_slot(
        hydra_available,
        hydra_available.slots[0],
    )
    before = extended_state_snapshot(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b3_22_abandon_then_different_booking",
        [
            f"عايزة أحجز {laser['name']}",
            "على كانديلا",
            f"يوم {laser_date}",
            "خلاص سيبي الحجز ده",
        ],
    )
    cleared = (
        load_active_task(
            db,
            workspace_id=workspace.id,
            conversation_id=conversation_id,
            patient_id=patient.id,
            run_id=None,
        )
        is None
    )
    _, fresh = send_turn(
        db,
        workspace,
        patient,
        "b3_22_abandon_then_different_booking",
        5,
        (
            f"طيب احجزيلي {hydra['name']} يوم {hydra_date} الساعة "
            f"{hydra_time} مع {doctor_name(hydra_doctor)}"
        ),
        conversation_id,
    )
    turns.append(fresh)
    after = extended_state_snapshot(db, workspace, patient)
    created = created_appointments(before, after)
    ok = (
        cleared
        and len(created) == 1
        and created[0]["service_id"] == str(hydra["id"])
        and created[0]["laser_device_key"] is None
    )
    return make_result(
        scenario_id="b3_22_abandon_then_different_booking",
        category="complex_multi_intent",
        purpose=(
            "Abandoning an unfinished laser task must clear it before a different "
            "fresh booking starts."
        ),
        turns=turns,
        before=before,
        after=after,
        verification={
            "active_task_cleared_after_abandon": cleared,
            "created": created,
        },
        deterministic_ok=ok,
        expected=(
            "Old laser/Candela task clears; fresh Hydrafacial booking contains no "
            "stale laser constraints."
        ),
        issue_title="Abandoned active task leaked into a different booking",
        issue_detail=(
            "Explicit abandonment must clear deterministic task ownership before "
            "the new booking intent."
        ),
    )


CASES: list[ScenarioFn] = [
    case_01_handoff_during_active_booking,
    case_02_staff_takeover_then_handback,
    case_03_financial_handoff_then_new_booking,
    case_04_lifecycle_while_human_owns,
    case_05_two_appointments_explicit_date,
    case_06_two_same_service_explicit_date,
    case_07_real_appointment_ambiguity,
    case_08_candidate_selection_followup,
    case_09_cancel_one_of_multiple,
    case_10_one_session_remaining,
    case_11_exhausted_package,
    case_12_expired_package,
    case_13_two_eligible_packages,
    case_14_package_booking_then_cancel,
    case_15_package_booking_then_reschedule,
    case_16_old_cancelled_workflow_isolation,
    case_17_old_doctor_preference_isolation,
    case_18_old_device_isolation,
    case_19_price_package_booking,
    case_20_side_query_during_reschedule,
    case_21_repeated_corrections,
    case_22_abandon_then_different_booking,
]


def run_case(
    engine,
    workspace_slug: str,
    case_fn: ScenarioFn,
) -> ScenarioResult:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        acquire_eval_advisory_lock(
            db,
            namespace="tia-agent-eval-batch-03",
        )
        workspace = db.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
        if workspace is None:
            raise RuntimeError("Workspace not found")
        assert_demo_only(workspace)
        _ensure_batch2_catalog_fixtures(db, workspace)
        return case_fn(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult(
            id=case_fn.__name__.removeprefix("case_"),
            category="infrastructure",
            purpose="Scenario execution failed before review.",
            turns=[],
            state_before={},
            state_after={},
            db_verification={},
            evaluation=default_evaluation(
                db_ok=False,
                grounding_ok=False,
            ),
            issues=[],
            token_usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "uncached_input_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
                "metadata_missing_calls": 0,
            },
            execution_error=f"{type(exc).__name__}: {exc}",
            review={
                "status": "INFRASTRUCTURE_FAILURE",
                "expected": "",
                "observed": {},
                "reviewer_notes": f"{type(exc).__name__}: {exc}",
                "severity": None,
                "root_cause": ("Infrastructure/provider noise or test-data problem"),
            },
        )
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def _stage_metrics(results: list[ScenarioResult]) -> dict[str, Any]:
    calls = [call for row in results for turn in row.turns for call in turn.llm_calls]

    def selected(*names: str):
        allowed = set(names)
        return [call for call in calls if call.get("operation") in allowed]

    def sums(rows):
        return {
            "calls": len(rows),
            "input_tokens": sum(
                int(call.get("input_tokens_actual") or 0) for call in rows
            ),
            "cached_read_tokens": sum(
                int(call.get("cached_tokens_actual") or 0) for call in rows
            ),
            "cache_write_tokens": sum(
                int(call.get("cache_write_tokens_actual") or 0) for call in rows
            ),
            "uncached_tokens": sum(
                int(call.get("uncached_input_tokens_actual") or 0) for call in rows
            ),
            "output_tokens": sum(
                int(call.get("output_tokens_actual") or 0) for call in rows
            ),
            "latency_ms": sum(int(call.get("latency_ms") or 0) for call in rows),
            "retries": sum(int(call.get("retry_count") or 0) for call in rows),
            "fallback_calls": sum(bool(call.get("fallback_used")) for call in rows),
        }

    return {
        "interpreter": sums(selected("v2-turn-interpreter")),
        "responder": sums(selected("v2-customer-responder", "v2-responder")),
        "all": sums(calls),
        "turn_latency_ms": sum(
            turn.latency_ms for row in results for turn in row.turns
        ),
    }


def summarize(results: list[ScenarioResult]) -> dict[str, Any]:
    tokens = batch_token_summary(results)
    stage = _stage_metrics(results)
    return {
        "scenarios_run": len(results),
        "pending_manual_review": sum(
            row.review.get("status") == "PENDING_MANUAL_REVIEW" for row in results
        ),
        "infrastructure_failures": sum(
            row.execution_error is not None for row in results
        ),
        "deterministic_P0": sum(
            issue.get("severity") == "P0" for row in results for issue in row.issues
        ),
        "deterministic_P1": sum(
            issue.get("severity") == "P1" for row in results for issue in row.issues
        ),
        "deterministic_P2": sum(
            issue.get("severity") == "P2" for row in results for issue in row.issues
        ),
        "tokens": tokens,
        "total_turns": sum(len(row.turns) for row in results),
        "total_llm_calls": stage["all"]["calls"],
        "interpreter_calls": stage["interpreter"]["calls"],
        "responder_calls": stage["responder"]["calls"],
        "stage_metrics": stage,
    }


def write_reports(
    payload: dict[str, Any],
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            jsonable(payload),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Linka Agent Evaluation — Batch 03 Raw Baseline",
        "",
        (f"- Batch runtime base SHA: {payload['run_metadata']['batch3_base_sha']}"),
        f"- Harness SHA: {payload['run_metadata']['git_sha']}",
        (f"- Scenario version: {payload['run_metadata']['scenario_version']}"),
        f"- Model: {payload['run_metadata']['model']}",
        f"- Reasoning: {payload['run_metadata']['reasoning_effort']}",
        (f"- Scenarios executed: {len(payload['scenario_results'])}"),
        "",
        (
            "Raw results are intentionally unreviewed. Deterministic findings "
            "are guards, not final PASS/FAIL."
        ),
        "",
    ]
    for row in payload["scenario_results"]:
        lines.extend(
            [
                f"## {row['id']}",
                "",
                f"Category: {row['category']}",
                f"Purpose: {row['purpose']}",
                f"Review status: {row['review']['status']}",
                f"Expected: {row['review']['expected']}",
                "",
            ]
        )
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
        lines.append(
            json.dumps(
                row["db_verification"],
                ensure_ascii=False,
                indent=2,
            )
        )
        if row["issues"]:
            lines.append("Deterministic findings:")
            for issue in row["issues"]:
                lines.append(
                    f"- {issue['severity']}: {issue['title']} — {issue['detail']}"
                )
        else:
            lines.append("Deterministic findings: none; manual review still required.")
        lines.append("")
    lines.extend(
        [
            "## Batch summary",
            "",
            json.dumps(
                payload["batch_summary"],
                ensure_ascii=False,
                indent=2,
            ),
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ns = parse_args()
    require_explicit_demo_eval()
    if not all(
        value > 0
        for value in (
            ns.input_price_per_million,
            ns.cached_input_price_per_million,
            ns.output_price_per_million,
        )
    ):
        raise RuntimeError("Current provider pricing must be supplied explicitly.")

    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
    )
    with Session(engine) as db:
        workspace = db.scalar(
            select(Workspace).where(Workspace.slug == ns.workspace_slug)
        )
        if workspace is None:
            raise RuntimeError("Demo workspace not found.")
        assert_demo_only(workspace)
        demo_seed = {
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "history_loader_limit": settings.agent_history_messages,
            "fixture_version": BATCH3_FIXTURE_VERSION,
        }

    results: list[ScenarioResult] = []
    stopped_for_p0 = False
    for case_fn in CASES:
        row = run_case(
            engine,
            ns.workspace_slug,
            case_fn,
        )
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
    total_actual_cost = sum(
        float(row.cost.get("actual_total_usd") or 0) for row in results
    )
    total_without_cache = sum(
        float(row.cost.get("without_explicit_cache_usd") or 0) for row in results
    )
    summary["cost"] = {
        "actual_usd": round(total_actual_cost, 8),
        "without_explicit_cache_usd": round(
            total_without_cache,
            8,
        ),
        "saving_usd": round(
            max(
                0.0,
                total_without_cache - total_actual_cost,
            ),
            8,
        ),
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
            "batch3_base_sha": ns.batch3_base_sha,
            "workspace": ns.workspace_slug,
            "model": settings.openai_model,
            "fallback_model": settings.openai_fallback_model,
            "reasoning_effort": settings.openai_reasoning_effort,
            "fallback_reasoning_effort": (settings.openai_fallback_reasoning_effort),
            "generated_at": datetime.now(UTC).isoformat(),
            "pricing": {
                "input_per_million": (ns.input_price_per_million),
                "cached_input_per_million": (ns.cached_input_price_per_million),
                "output_per_million": (ns.output_price_per_million),
                "cache_write_multiplier": (ns.cache_write_multiplier),
                "source": ns.pricing_source,
            },
            "demo_seed": demo_seed,
        },
        "scenario_results": [jsonable(row) for row in results],
        "batch_summary": summary,
    }
    json_path = output_dir / f"batch_03_raw_{timestamp}.json"
    md_path = output_dir / f"batch_03_raw_{timestamp}.md"
    write_reports(
        payload,
        json_path,
        md_path,
    )

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
                    "structured_operations": [
                        operation
                        for trace in (turn.get("structured_trace") or [])
                        for operation in (
                            (trace.get("understanding") or {}).get("operations") or []
                        )
                    ],
                    "plan_reads": [
                        read
                        for trace in (turn.get("structured_trace") or [])
                        for step in ((trace.get("plan") or {}).get("steps") or [])
                        for read in (step.get("reads") or [])
                    ],
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

    encoded = base64.b64encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).decode("ascii")
    print("EVAL_REPORT_B64_BEGIN", flush=True)
    for offset in range(
        0,
        len(encoded),
        3000,
    ):
        print(
            f"EVAL_REPORT_B64={encoded[offset : offset + 3000]}",
            flush=True,
        )
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
