from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import scripts.run_live_agent_ux_review as base
import v2_benchmark_core as core
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.service import Service
from app.models.workspace import Workspace

REPORT = Path("artifacts/cancellation-multilaser-audit.json")


def _contains(value: Any, key: str, expected: str) -> bool:
    if isinstance(value, dict):
        if str(value.get(key) or "") == expected:
            return True
        return any(_contains(item, key, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains(item, key, expected) for item in value)
    return False


def _appointment_rows(db: Session, workspace: Workspace, patient: Patient) -> list[Appointment]:
    return list(
        db.scalars(
            select(Appointment)
            .where(
                Appointment.workspace_id == workspace.id,
                Appointment.patient_id == patient.id,
            )
            .order_by(Appointment.start_at.asc(), Appointment.id.asc())
        )
    )


def _snapshot(db: Session, workspace: Workspace, patient: Patient) -> dict[str, dict[str, Any]]:
    return {
        str(row.id): {
            "status": row.status,
            "service_id": str(row.service_id),
            "doctor_id": str(row.doctor_id),
            "start_at": row.start_at.isoformat(),
            "visit_group_id": str(row.visit_group_id) if row.visit_group_id else None,
            "laser_device_key": row.laser_device_key,
        }
        for row in _appointment_rows(db, workspace, patient)
    }


def _turn_payload(turn: core.TurnRecord) -> dict[str, Any]:
    return {
        "customer": turn.customer,
        "assistant": turn.assistant,
        "model": turn.model,
        "understanding": turn.understanding,
        "plan": turn.plan,
        "outcomes": turn.outcomes,
        "traces": turn.traces,
        "state_after": turn.state_after,
    }


def _run_messages(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    messages: list[str],
) -> list[core.TurnRecord]:
    turns: list[core.TurnRecord] = []
    conversation_id = None
    for message in messages:
        turn, conversation_id = core.send(db, workspace, patient, message, conversation_id)
        turns.append(turn)
    return turns


def _service(db: Session, workspace: Workspace, name: str) -> Service:
    row = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.is_active.is_(True),
            Service.name == name,
        )
    )
    if row is None:
        raise RuntimeError(f"Missing synthetic service: {name}")
    return row


def _patient_by_phone(db: Session, workspace: Workspace, phone: str) -> Patient:
    row = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace.id,
            Patient.phone_normalized == phone,
        )
    )
    if row is None:
        raise RuntimeError(f"Missing synthetic patient: {phone}")
    return row


def _new_rows(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    before: dict[str, dict[str, Any]],
) -> list[Appointment]:
    return [row for row in _appointment_rows(db, workspace, patient) if str(row.id) not in before]


def _rows_json(rows: list[Appointment]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row.id),
            "status": row.status,
            "service_id": str(row.service_id),
            "doctor_id": str(row.doctor_id),
            "start_at": row.start_at.isoformat(),
            "end_at": row.end_at.isoformat(),
            "visit_group_id": str(row.visit_group_id) if row.visit_group_id else None,
            "laser_device_key": row.laser_device_key,
        }
        for row in rows
    ]


def _cancel_case(db: Session, workspace: Workspace, count: int, *, followup: bool = False) -> dict[str, Any]:
    patient = core.patient_without_future(db, workspace)
    base._seed_upcoming(db, workspace, patient, count)
    before = _snapshot(db, workspace, patient)
    messages = ["عايز ألغي ميعادي الجاي دلوقتي"]
    if followup:
        messages.append("التاني")
    turns = _run_messages(db, workspace, patient, messages)
    after = _snapshot(db, workspace, patient)
    handoff = any(_contains(turn.outcomes, "status", "handoff") or _contains(turn.plan, "disposition", "handoff") for turn in turns)
    return {
        "appointment_count": count,
        "handoff_seen": handoff,
        "before": before,
        "after": after,
        "business_unchanged": before == after,
        "turns": [_turn_payload(turn) for turn in turns],
    }


def ca1(db: Session, workspace: Workspace) -> dict[str, Any]:
    patient = core.patient_without_future(db, workspace)
    before = _snapshot(db, workspace, patient)
    turns = _run_messages(db, workspace, patient, ["عايز ألغي ميعادي الجاي دلوقتي"])
    after = _snapshot(db, workspace, patient)
    return {
        "pass": before == after and len(before) == 0,
        "before": before,
        "after": after,
        "turns": [_turn_payload(turn) for turn in turns],
    }


def ca2(db: Session, workspace: Workspace) -> dict[str, Any]:
    patient = core.patient_without_future(db, workspace)
    rows = base._seed_upcoming(db, workspace, patient, 1)
    target = rows[0]
    before = _snapshot(db, workspace, patient)
    turns = _run_messages(
        db,
        workspace,
        patient,
        ["فكرني بمعادي الجاي", "تمام، الغيه دلوقتي"],
    )
    db.refresh(target)
    after = _snapshot(db, workspace, patient)
    return {
        "pass": target.status == "cancelled",
        "target_id": str(target.id),
        "target_status": target.status,
        "before": before,
        "after": after,
        "turns": [_turn_payload(turn) for turn in turns],
    }


def ca3(db: Session, workspace: Workspace) -> dict[str, Any]:
    data = _cancel_case(db, workspace, 2)
    data["pass"] = data["business_unchanged"] and data["handoff_seen"]
    return data


def ca4(db: Session, workspace: Workspace) -> dict[str, Any]:
    data = _cancel_case(db, workspace, 3)
    data["pass"] = data["business_unchanged"] and data["handoff_seen"]
    return data


def ca5(db: Session, workspace: Workspace) -> dict[str, Any]:
    data = _cancel_case(db, workspace, 2, followup=True)
    data["pass"] = data["business_unchanged"] and data["handoff_seen"]
    return data


def ca6(db: Session, workspace: Workspace) -> dict[str, Any]:
    patient = _patient_by_phone(db, workspace, "+200000230006")
    before = _snapshot(db, workspace, patient)
    service = _service(db, workspace, "تنظيف بشرة عميق")
    target_ids = [row_id for row_id, item in before.items() if item["service_id"] == str(service.id)]
    turns = _run_messages(
        db,
        workspace,
        patient,
        ["عايز ألغي موعد تنظيف بشرة عميق بس، متلغيش أي موعد تاني", "أيوه الغيه"],
    )
    after = _snapshot(db, workspace, patient)
    cancelled_targets = [row_id for row_id in target_ids if after.get(row_id, {}).get("status") == "cancelled"]
    unrelated_unchanged = all(
        before[row_id]["status"] == after.get(row_id, {}).get("status")
        for row_id in before
        if row_id not in target_ids
    )
    return {
        "pass": len(target_ids) == 1 and len(cancelled_targets) == 1 and unrelated_unchanged,
        "target_ids": target_ids,
        "cancelled_targets": cancelled_targets,
        "unrelated_unchanged": unrelated_unchanged,
        "before": before,
        "after": after,
        "turns": [_turn_payload(turn) for turn in turns],
    }


def ca7(db: Session, workspace: Workspace) -> dict[str, Any]:
    patient = _patient_by_phone(db, workspace, "+200000230006")
    before = _snapshot(db, workspace, patient)
    turns = _run_messages(db, workspace, patient, ["عايز ألغي ميعادي الجاي"])
    after = _snapshot(db, workspace, patient)
    handoff = any(_contains(turn.outcomes, "status", "handoff") or _contains(turn.plan, "disposition", "handoff") for turn in turns)
    return {
        "pass": before == after and handoff and len(before) >= 2,
        "handoff_seen": handoff,
        "before": before,
        "after": after,
        "turns": [_turn_payload(turn) for turn in turns],
    }


def ca9(db: Session, workspace: Workspace) -> dict[str, Any]:
    current = core.patient_without_future(db, workspace)
    other = db.scalar(
        select(Patient)
        .where(
            Patient.workspace_id == workspace.id,
            Patient.status != "blocked",
            Patient.id != current.id,
        )
        .order_by(Patient.created_at.asc())
        .limit(1)
    )
    if other is None:
        raise RuntimeError("Need second synthetic patient")
    current_target = base._seed_upcoming(db, workspace, current, 1)[0]
    other_target = base._seed_upcoming(db, workspace, other, 1)[0]
    other_before = other_target.status
    turns = _run_messages(db, workspace, current, ["عايز ألغي ميعادي الجاي دلوقتي"])
    db.refresh(current_target)
    db.refresh(other_target)
    return {
        "pass": other_target.status == other_before,
        "current_status": current_target.status,
        "other_status_before": other_before,
        "other_status_after": other_target.status,
        "turns": [_turn_payload(turn) for turn in turns],
    }


def _multi_laser(
    db: Session,
    workspace: Workspace,
    services: list[str],
    messages: list[str] | None = None,
) -> dict[str, Any]:
    patient = core.patient_without_future(db, workspace)
    service_rows = [_service(db, workspace, name) for name in services]
    before = _snapshot(db, workspace, patient)
    if messages is None:
        joined = " و".join(services)
        messages = [
            f"عايزة أحجز الخدمات دي كخدمات منفصلة في نفس الزيارة: {joined}. اختار دكتور واحد يعملهم كلهم ووريني أقرب اختيار مناسب، متحجزش لسه",
            "تمام احجز كل الخدمات دي مع بعض في نفس الزيارة على أقرب اختيار مناسب اللي لقيته",
        ]
    turns = _run_messages(db, workspace, patient, messages)
    created = _new_rows(db, workspace, patient, before)
    requested_ids = {str(row.id) for row in service_rows}
    created_ids = {str(row.service_id) for row in created}
    return {
        "requested_services": services,
        "requested_service_ids": sorted(requested_ids),
        "created": _rows_json(created),
        "all_requested_created": requested_ids.issubset(created_ids),
        "no_duplicate_services": len(created_ids) == len(created),
        "visit_group_ids": sorted({str(row.visit_group_id) for row in created if row.visit_group_id}),
        "doctor_ids": sorted({str(row.doctor_id) for row in created}),
        "starts": sorted({row.start_at.isoformat() for row in created}),
        "turns": [_turn_payload(turn) for turn in turns],
    }


def ml1(db: Session, workspace: Workspace) -> dict[str, Any]:
    data = _multi_laser(db, workspace, ["ليزر إزالة الشعر - إبط", "ليزر إزالة الشعر - بكيني"])
    data["pass"] = data["all_requested_created"] and data["no_duplicate_services"]
    return data


def ml2(db: Session, workspace: Workspace) -> dict[str, Any]:
    data = _multi_laser(
        db,
        workspace,
        ["ليزر إزالة الشعر - إبط", "ليزر إزالة الشعر - بكيني", "ليزر إزالة الشعر - ذراعين كاملين"],
    )
    data["pass"] = data["all_requested_created"] and data["no_duplicate_services"]
    return data


def ml6(db: Session, workspace: Workspace) -> dict[str, Any]:
    patient = core.patient_without_future(db, workspace)
    before = _snapshot(db, workspace, patient)
    turns = _run_messages(
        db,
        workspace,
        patient,
        [
            "عايزة أحجز ليزر إزالة الشعر - إبط وليزر إزالة الشعر - بكيني مع د. سارة عادل تحديدًا في نفس الزيارة، نفذ الحجز لو هي بتعمل الخدمتين"
        ],
    )
    after = _snapshot(db, workspace, patient)
    return {
        "pass": before == after,
        "before": before,
        "after": after,
        "turns": [_turn_payload(turn) for turn in turns],
    }


def ml9(db: Session, workspace: Workspace) -> dict[str, Any]:
    services = ["ليزر إزالة الشعر - إبط", "ليزر إزالة الشعر - ذراعين كاملين"]
    messages = [
        "عايزة أحجز ليزر إزالة الشعر - إبط وليزر إزالة الشعر - بكيني في نفس الزيارة، وريني أقرب اختيار ومتحجزش",
        "لا شيل البكيني وخلي بدلها ليزر إزالة الشعر - ذراعين كاملين، ولسه متحجزش",
        "تمام احجز الإبط والذراعين كاملين مع بعض في نفس الزيارة",
    ]
    data = _multi_laser(db, workspace, services, messages)
    bikini = _service(db, workspace, "ليزر إزالة الشعر - بكيني")
    created_ids = {row["service_id"] for row in data["created"]}
    data["pass"] = data["all_requested_created"] and str(bikini.id) not in created_ids
    return data


def ml10(db: Session, workspace: Workspace) -> dict[str, Any]:
    services = ["ليزر إزالة الشعر - إبط", "ليزر إزالة الشعر - بكيني"]
    messages = [
        "عايزة أحجز ليزر إزالة الشعر - إبط وليزر إزالة الشعر - بكيني في نفس الزيارة، وريني أقرب اختيار ومتحجزش",
        "على جنب كده، سعر الخدمتين كام؟ متحجزش لسه",
        "تمام نكمل واحجز الخدمتين مع بعض في نفس الزيارة على الاختيار اللي لقيته",
    ]
    data = _multi_laser(db, workspace, services, messages)
    data["pass"] = data["all_requested_created"]
    return data


def ml11(db: Session, workspace: Workspace) -> dict[str, Any]:
    patient = core.patient_without_future(db, workspace)
    underarm = _service(db, workspace, "ليزر إزالة الشعر - إبط")
    bikini = _service(db, workspace, "ليزر إزالة الشعر - بكيني")
    before = _snapshot(db, workspace, patient)
    turns = _run_messages(
        db,
        workspace,
        patient,
        ["سعر ليزر إزالة الشعر - إبط وسعر ليزر إزالة الشعر - بكيني كخدمتين منفصلتين كام؟ أنا بس بسأل ومتحجزش"],
    )
    after = _snapshot(db, workspace, patient)
    reply = " ".join((turn.assistant or "") for turn in turns)
    expected = [str(underarm.price_minor // 100), str(bikini.price_minor // 100)]
    return {
        "pass": before == after and all(value in reply for value in expected),
        "expected_prices_egp": expected,
        "reply": reply,
        "business_unchanged": before == after,
        "turns": [_turn_payload(turn) for turn in turns],
    }


CASES: list[tuple[str, Callable[[Session, Workspace], dict[str, Any]]]] = [
    ("CA1", ca1),
    ("CA2", ca2),
    ("CA3", ca3),
    ("CA4", ca4),
    ("CA5", ca5),
    ("CA6", ca6),
    ("CA7", ca7),
    ("CA9", ca9),
    ("ML1", ml1),
    ("ML2", ml2),
    ("ML6", ml6),
    ("ML9", ml9),
    ("ML10", ml10),
    ("ML11", ml11),
]


def execute(engine, name: str, func: Callable[[Session, Workspace], dict[str, Any]]) -> dict[str, Any]:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        if workspace is None:
            raise RuntimeError("Workspace tia not found")
        data = func(db, workspace)
        return {"name": name, "error": None, "data": data}
    except Exception as exc:
        return {"name": name, "error": f"{type(exc).__name__}: {exc}", "data": {"pass": False}}
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def main() -> int:
    if not settings.agent_v2_live_enabled:
        raise RuntimeError("Audit requires AGENT_V2_LIVE_ENABLED=true")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    results = []
    for name, func in CASES:
        print(f"[audit] {name}", flush=True)
        result = execute(engine, name, func)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    engine.dispose()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    cancellation_failures = [
        row for row in results if row["name"].startswith("CA") and (row["error"] or not row["data"].get("pass"))
    ]
    # Multi-laser findings are audit-only in PR #237. Do not fail the cancellation implementation
    # solely because a multi-laser defect is discovered; the report preserves that evidence.
    return 1 if cancellation_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
