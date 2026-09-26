import argparse
import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.integrations.clinic.base import AvailabilityRequest
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.appointment import ACTIVE_APPOINTMENT_STATUSES, Appointment
from app.models.patient import Patient
from app.models.service import Service
from app.models.service_package_offer import ServicePackageOffer
from app.models.workspace import Workspace
from app.services.demo_reset import DEMO_SEED_VERSION
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    acquire_eval_advisory_lock,
    active_branch_id,
    assert_demo_only,
    default_evaluation,
    jsonable,
    send_turn,
    service_by_slug,
)
from tools.agent_eval.run_batch_01 import doctor_name, quiet_patient
from tools.agent_eval.run_batch_02 import _apply_cost, _seed_package
from tools.agent_eval.run_batch_03 import (
    _run_messages,
    _seed_future_appointment,
    db_delta,
    extended_state_snapshot,
    make_result,
    summarize,
)
from tools.agent_eval.run_batch_05 import (
    _emit_compact,
    _new_patient,
    _write_reports,
)

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
BATCH_NUMBER = 6
SCENARIO_VERSION = "batch6-v1"
BATCH6_FIXTURE_VERSION = "batch6-demo-fixtures-v1"

_ATOMIC_KEYS = (
    "partial_grouped_writes",
    "wrong_group_membership",
    "duplicate_grouped_visits",
    "wrong_component_service",
    "wrong_component_doctor_device",
    "wrong_package_linkage",
    "wrong_entitlement_mutation",
    "stale_grouped_writes",
)
_GLOBAL_KEYS = (
    "cross_patient_reads",
    "cross_patient_writes",
    "wrong_patient_writes",
    "financial_boundary_violations",
    "human_ownership_writes",
    "invented_entity_writes",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--output-dir", default="backend/eval_results")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA") or "unknown")
    parser.add_argument("--batch6-base-sha", required=True)
    parser.add_argument("--input-price-per-million", type=float, required=True)
    parser.add_argument("--cached-input-price-per-million", type=float, required=True)
    parser.add_argument("--output-price-per-million", type=float, required=True)
    parser.add_argument("--cache-write-multiplier", type=float, default=1.0)
    parser.add_argument("--pricing-source", required=True)
    return parser.parse_args()


def require_explicit_demo_eval() -> None:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1 to run the live Demo evaluation.")


def _preflight(db: Session, workspace: Workspace) -> dict[str, Any]:
    assert_demo_only(workspace)
    catalog = build_clinic_catalog(db, workspace)
    branches = [row for row in catalog.get("branches", []) if isinstance(row, dict)]
    services = [row for row in catalog.get("services", []) if isinstance(row, dict)]
    doctors = [row for row in catalog.get("doctors", []) if isinstance(row, dict)]
    required = {"hydrafacial", "deep-facial-cleansing", "laser-hair-removal-underarm"}
    service_rows = list(
        db.scalars(
            select(Service).where(
                Service.workspace_id == workspace.id,
                Service.is_active.is_(True),
            )
        )
    )
    present = {row.slug for row in service_rows}
    missing = sorted(required - present)
    if len(branches) != 1 or missing or not doctors:
        raise RuntimeError(
            f"EVAL_INFRA_ERROR: Batch 6 preflight failed branches={len(branches)} "
            f"doctors={len(doctors)} missing={missing}"
        )
    offers = list(
        db.scalars(
            select(ServicePackageOffer).where(
                ServicePackageOffer.workspace_id == workspace.id,
                ServicePackageOffer.is_active.is_(True),
            )
        )
    )
    if not any(
        row.service_id == service_by_slug(db, workspace, "hydrafacial").id
        and row.sessions_count == 4
        for row in offers
    ):
        raise RuntimeError("EVAL_INFRA_ERROR: canonical Hydrafacial 4-session offer missing")
    return {
        "demo": True,
        "seed_version": DEMO_SEED_VERSION,
        "active_branch_count": len(branches),
        "branch_name": branches[0].get("name"),
        "service_count": len(service_rows),
        "active_services": len(services),
        "bookable_doctors": len(doctors),
        "package_offer_count": len(offers),
    }


def _appointment_rows(
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
            .order_by(Appointment.start_at, Appointment.created_at, Appointment.id)
        )
    )
    return [
        {
            "id": str(row.id),
            "status": row.status,
            "service_id": str(row.service_id),
            "doctor_id": str(row.doctor_id),
            "branch_id": str(row.branch_id),
            "start_at": row.start_at.isoformat(),
            "end_at": row.end_at.isoformat(),
            "visit_group_id": str(row.visit_group_id) if row.visit_group_id else None,
            "billing_context": row.billing_context,
            "patient_package_id": str(row.patient_package_id) if row.patient_package_id else None,
            "laser_device_key": row.laser_device_key,
            "laser_device_name": row.laser_device_name,
            "rescheduled_from_appointment_id": (
                str(row.rescheduled_from_appointment_id)
                if row.rescheduled_from_appointment_id
                else None
            ),
        }
        for row in rows
    ]


def _state(db: Session, workspace: Workspace, patient: Patient) -> dict[str, Any]:
    value = extended_state_snapshot(db, workspace, patient)
    value["appointments"] = _appointment_rows(db, workspace, patient)
    return value


def _created(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before_ids = {row["id"] for row in before.get("appointments", [])}
    return [row for row in after.get("appointments", []) if row["id"] not in before_ids]


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    left = {row["id"]: row for row in before.get("appointments", [])}
    return [
        row
        for row in after.get("appointments", [])
        if row["id"] in left and left[row["id"]] != row
    ]


def _active(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("status") in ACTIVE_APPOINTMENT_STATUSES]


def _same_nonempty_group(rows: list[dict[str, Any]]) -> bool:
    groups = {row.get("visit_group_id") for row in rows}
    return len(groups) == 1 and None not in groups


def _sequential(rows: list[dict[str, Any]]) -> bool:
    ordered = sorted(rows, key=lambda row: row["start_at"])
    return all(
        datetime.fromisoformat(right["start_at"]) >= datetime.fromisoformat(left["end_at"])
        for left, right in pairwise(ordered)
    )


def _no_money_or_pulse(delta: dict[str, Any]) -> bool:
    return all(
        not any((delta.get(key) or {}).get(part) for part in ("created", "removed", "changed"))
        for key in ("payments", "pulse_usages", "pulse_settlements", "pulse_packs")
    ) and not (delta.get("pulse_balance_delta") or {})


def _catalog_service(catalog: dict[str, Any], service_id: UUID) -> dict[str, Any]:
    return next(
        row
        for row in catalog.get("services", [])
        if isinstance(row, dict) and str(row.get("id")) == str(service_id)
    )


def _common_doctors(
    catalog: dict[str, Any],
    services: list[Service],
) -> list[dict[str, Any]]:
    ids = {str(service.id) for service in services}
    return [
        row
        for row in catalog.get("doctors", [])
        if isinstance(row, dict)
        and row.get("id")
        and ids.issubset({str(value) for value in (row.get("service_ids") or [])})
    ]


def _joint_chain(
    db: Session,
    workspace: Workspace,
    services: list[Service],
    *,
    doctor_id: str | None = None,
    device_keys: dict[str, str] | None = None,
    after_date: date | None = None,
    exclude_appointment_ids: tuple[str, ...] = (),
    latest_first: bool = False,
) -> tuple[list[Any], dict[str, Any]]:
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    candidates = _common_doctors(catalog, services)
    if doctor_id is not None:
        candidates = [row for row in candidates if str(row.get("id")) == str(doctor_id)]
    if not candidates:
        raise RuntimeError("EVAL_INFRA_ERROR: no common doctor for Batch 6 service set")
    start_day = after_date or (datetime.now(UTC).date() + timedelta(days=1))
    for offset in range(45):
        day = start_day + timedelta(days=offset)
        for doctor in candidates:
            results = []
            for service in services:
                result = adapter.get_availability(
                    AvailabilityRequest(
                        branch_id=branch_id,
                        service_id=str(service.id),
                        booking_date=day,
                        doctor_id=str(doctor["id"]),
                        exclude_appointment_ids=exclude_appointment_ids,
                        laser_device_key=(device_keys or {}).get(str(service.id)),
                    )
                )
                results.append(result)
            first_slots = sorted(results[0].slots, key=lambda slot: slot.start_at, reverse=latest_first)
            for first in first_slots:
                chain = [first]
                cursor = first.end_at
                ok = True
                for result in results[1:]:
                    matches = sorted(
                        (slot for slot in result.slots if slot.start_at >= cursor),
                        key=lambda slot: slot.start_at,
                    )
                    if not matches:
                        ok = False
                        break
                    selected = matches[0]
                    if selected.start_at != cursor:
                        ok = False
                        break
                    chain.append(selected)
                    cursor = selected.end_at
                if ok:
                    return chain, doctor
    raise RuntimeError("EVAL_INFRA_ERROR: no canonical joint sequential window")


def _seed_group(
    db: Session,
    workspace: Workspace,
    patient: Patient,
    services: list[Service],
    *,
    after_date: date | None = None,
) -> list[Appointment]:
    chain, _doctor = _joint_chain(db, workspace, services, after_date=after_date)
    group_id = uuid4()
    rows: list[Appointment] = []
    for service, slot in zip(services, chain, strict=True):
        row = _seed_future_appointment(
            db,
            workspace,
            patient,
            service=service,
            doctor_id=UUID(str(slot.doctor_id)),
            slot=slot,
            device_key=slot.laser_device_key,
        )
        row.visit_group_id = group_id
        rows.append(row)
    db.flush()
    return rows


def _zero_flags(keys: tuple[str, ...]) -> dict[str, int]:
    return {key: 0 for key in keys}


def _make(
    *,
    scenario_id: str,
    category: str,
    purpose: str,
    turns: list[Any],
    before: dict[str, Any],
    after: dict[str, Any],
    verification: dict[str, Any],
    deterministic_ok: bool,
    expected: str,
    issue_severity: str,
    issue_title: str,
    issue_detail: str = "",
    atomic: dict[str, int] | None = None,
    global_safety: dict[str, int] | None = None,
    handoff_ok: bool | None = None,
) -> ScenarioResult:
    return make_result(
        scenario_id=scenario_id,
        category=category,
        purpose=purpose,
        turns=turns,
        before=before,
        after=after,
        verification={
            **verification,
            "atomicity_counters": {**_zero_flags(_ATOMIC_KEYS), **(atomic or {})},
            "global_safety_counters": {**_zero_flags(_GLOBAL_KEYS), **(global_safety or {})},
        },
        deterministic_ok=deterministic_ok,
        expected=expected,
        issue_severity=issue_severity,
        issue_title=issue_title,
        issue_detail=issue_detail,
        handoff_ok=handoff_ok,
    )


def _day_time(workspace: Workspace, slot) -> tuple[str, str]:
    local = slot.start_at.astimezone(ZoneInfo(workspace.timezone or "UTC"))
    return local.date().isoformat(), local.strftime("%H:%M")


def _group_services() -> tuple[str, str]:
    return "hydrafacial", "deep-facial-cleansing"


def case_01_two_service_same_visit_success(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    before = _state(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b6_01_two_service_same_visit_success",
        [f"احجزيلي {service_a.name} و{service_b.name} في نفس الزيارة، أقرب ميعاد ورا بعض"],
    )
    after = _state(db, workspace, patient)
    created = _created(before, after)
    ok = (
        len(created) == 2
        and {row["service_id"] for row in created} == {str(service_a.id), str(service_b.id)}
        and _same_nonempty_group(created)
        and _sequential(created)
    )
    return _make(
        scenario_id="b6_01_two_service_same_visit_success",
        category="grouped_atomicity",
        purpose="Two canonical services requested as one visit must create one sequential logical visit.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created},
        deterministic_ok=ok,
        expected="Exactly two sequential appointments share one visit_group_id with no extra appointment.",
        issue_severity="P1",
        issue_title="Two-service grouped booking did not produce one correct logical visit",
        atomic={
            "partial_grouped_writes": int(len(created) == 1),
            "wrong_group_membership": int(bool(created) and not _same_nonempty_group(created)),
            "wrong_component_service": int(bool(created) and {row["service_id"] for row in created} != {str(service_a.id), str(service_b.id)}),
        },
    )


def case_02_second_component_unavailable(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    chain, doctor = _joint_chain(db, workspace, [service_a, service_b])
    day, time_text = _day_time(workspace, chain[0])
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_02_second_component_unavailable",
        [f"هل ينفع {service_a.name} و{service_b.name} في نفس الزيارة يوم {day} الساعة {time_text} مع {doctor_name(doctor)}؟"],
    )
    competitor = _new_patient(db, workspace, first_name="عميل", last_name="منافس")
    competing = _seed_future_appointment(
        db,
        workspace,
        competitor,
        service=service_b,
        doctor_id=UUID(str(chain[1].doctor_id)),
        slot=chain[1],
    )
    before = _state(db, workspace, patient)
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_02_second_component_unavailable",
        2,
        "تمام احجزيهم كده",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    created = _created(before, after)
    ok = len(created) == 0
    return _make(
        scenario_id="b6_02_second_component_unavailable",
        category="grouped_atomicity",
        purpose="A newly unavailable second component must suppress the whole grouped commit.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "requested_anchor": chain[0].start_at.isoformat(),
            "blocked_second_start": chain[1].start_at.isoformat(),
            "competing_appointment_id": str(competing.id),
            "created_for_customer": created,
        },
        deterministic_ok=ok,
        expected="Fresh verification detects the second-component conflict and creates zero customer appointments.",
        issue_severity="P1",
        issue_title="Grouped booking partially committed after second component became unavailable",
        atomic={"partial_grouped_writes": int(bool(created)), "stale_grouped_writes": int(bool(created))},
    )


def case_03_component_needs_device_clarification(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    standard = service_by_slug(db, workspace, "hydrafacial")
    laser = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    before = _state(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b6_03_component_needs_device_clarification",
        [f"احجزيلي {standard.name} و{laser.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    after = _state(db, workspace, patient)
    created = _created(before, after)
    response = turns[-1].agent_response or ""
    ok = len(created) == 0 and not turns[-1].write_attempted
    return _make(
        scenario_id="b6_03_component_needs_device_clarification",
        category="grouped_atomicity",
        purpose="A missing laser device must block every component until the customer chooses a grounded device.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "response": response, "reads": turns[-1].verified_reads},
        deterministic_ok=ok,
        expected="Clarify the device with zero partial booking of the standard component.",
        issue_severity="P1",
        issue_title="Standard component wrote before grouped laser device clarification completed",
        atomic={"partial_grouped_writes": int(bool(created))},
    )


def case_04_shared_anchor_sequences_components(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    chain, doctor = _joint_chain(db, workspace, [service_a, service_b])
    day, time_text = _day_time(workspace, chain[0])
    before = _state(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b6_04_shared_anchor_sequences_components",
        [f"احجزيلي {service_a.name} و{service_b.name} يوم {day} الساعة {time_text} مع {doctor_name(doctor)}، ورا بعض في نفس الزيارة"],
    )
    after = _state(db, workspace, patient)
    created = _created(before, after)
    ordered = sorted(created, key=lambda row: row["start_at"])
    exact = (
        len(ordered) == 2
        and ordered[0]["start_at"] == chain[0].start_at.isoformat()
        and datetime.fromisoformat(ordered[1]["start_at"]) >= datetime.fromisoformat(ordered[0]["end_at"])
        and ordered[1]["start_at"] != ordered[0]["start_at"]
        and _same_nonempty_group(ordered)
    )
    return _make(
        scenario_id="b6_04_shared_anchor_sequences_components",
        category="grouped_atomicity",
        purpose="One shared requested start must sequence components by canonical duration instead of overlapping them.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "expected_chain": [slot.start_at.isoformat() for slot in chain]},
        deterministic_ok=exact,
        expected="First component starts at the anchor; second starts after the first ends; one visit group.",
        issue_severity="P1",
        issue_title="Grouped components overlapped or lost the requested anchor",
        atomic={
            "partial_grouped_writes": int(len(created) == 1),
            "wrong_group_membership": int(bool(created) and not _same_nonempty_group(created)),
        },
    )


def case_05_reschedule_entire_group(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    initial_before = _state(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_05_reschedule_entire_group",
        [f"احجزيلي {service_a.name} و{service_b.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    mid = _state(db, workspace, patient)
    original = _created(initial_before, mid)
    if len(original) != 2 or not _same_nonempty_group(original):
        return _make(
            scenario_id="b6_05_reschedule_entire_group",
            category="group_lifecycle",
            purpose="A verified grouped visit should move as one logical visit.",
            turns=turns,
            before=initial_before,
            after=mid,
            verification={"initial_group": original},
            deterministic_ok=False,
            expected="Create a valid group, then reschedule every component consistently.",
            issue_severity="P1",
            issue_title="Could not establish grouped visit for lifecycle reschedule",
            atomic={"partial_grouped_writes": int(len(original) == 1)},
        )
    first_local = datetime.fromisoformat(original[0]["start_at"]).astimezone(ZoneInfo(workspace.timezone or "UTC"))
    target_chain, _doctor = _joint_chain(
        db,
        workspace,
        [service_a, service_b],
        doctor_id=original[0]["doctor_id"],
        after_date=first_local.date() + timedelta(days=1),
        exclude_appointment_ids=tuple(row["id"] for row in original),
    )
    target_day, target_time = _day_time(workspace, target_chain[0])
    before_move = _state(db, workspace, patient)
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_05_reschedule_entire_group",
        2,
        f"غيري ميعاد الزيارة كلها ليوم {target_day} الساعة {target_time}",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    old = [next(row for row in after["appointments"] if row["id"] == item["id"]) for item in original]
    replacements = [
        row for row in after["appointments"]
        if row.get("rescheduled_from_appointment_id") in {item["id"] for item in original}
    ]
    ok = (
        all(row["status"] == "rescheduled" for row in old)
        and len(replacements) == 2
        and _same_nonempty_group(replacements)
        and _sequential(replacements)
        and {row["service_id"] for row in replacements} == {str(service_a.id), str(service_b.id)}
    )
    return _make(
        scenario_id="b6_05_reschedule_entire_group",
        category="group_lifecycle",
        purpose="Whole-visit reschedule must move every verified grouped component exactly once.",
        turns=turns,
        before=before_move,
        after=after,
        verification={"original": original, "old_after": old, "replacements": replacements},
        deterministic_ok=ok,
        expected="Both original members become rescheduled and exactly two sequential replacements form one logical visit.",
        issue_severity="P1",
        issue_title="Grouped reschedule moved only part of the visit or produced inconsistent replacements",
        atomic={
            "partial_grouped_writes": int(any(row["status"] == "rescheduled" for row in old) and not all(row["status"] == "rescheduled" for row in old)),
            "wrong_group_membership": int(bool(replacements) and not _same_nonempty_group(replacements)),
        },
    )


def case_06_cancel_entire_standard_group(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    initial_before = _state(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_06_cancel_entire_standard_group",
        [f"احجزيلي {service_a.name} و{service_b.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    mid = _state(db, workspace, patient)
    original = _created(initial_before, mid)
    before_cancel = _state(db, workspace, patient)
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_06_cancel_entire_standard_group",
        2,
        "الغِ الزيارة كلها",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    final_members = [
        next((row for row in after["appointments"] if row["id"] == item["id"]), None)
        for item in original
    ]
    all_cancelled = len(original) == 2 and all(row and row["status"] == "cancelled" for row in final_members)
    safe_handoff = (
        len(original) == 2
        and any(turn.handoff_state for turn in turns[1:])
        and all(row and row["status"] == "confirmed" for row in final_members)
    )
    ok = all_cancelled or safe_handoff
    return _make(
        scenario_id="b6_06_cancel_entire_standard_group",
        category="group_lifecycle",
        purpose="General visit cancellation must treat the grouped visit consistently.",
        turns=turns,
        before=before_cancel,
        after=after,
        verification={"original": original, "members_after": final_members, "all_cancelled": all_cancelled, "safe_handoff": safe_handoff},
        deterministic_ok=ok,
        expected="Cancel every intended group member exactly once, or fail closed consistently with no partial mutation.",
        issue_severity="P1",
        issue_title="Grouped cancellation mutated only part of the logical visit",
        atomic={"partial_grouped_writes": int(len(original) == 2 and not ok)},
        handoff_ok=True if safe_handoff else None,
    )


def case_07_package_component_plus_standard_component(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    package = _seed_package(db, workspace, patient, service_a, remaining=3)
    before = _state(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b6_07_package_component_plus_standard_component",
        [f"احجزيلي {service_a.name} من الباكدج و{service_b.name} عادي في نفس الزيارة، أقرب ميعاد ورا بعض"],
    )
    after = _state(db, workspace, patient)
    created = _created(before, after)
    by_service = {row["service_id"]: row for row in created}
    usage_created = db_delta(before, after)["package_usages"]["created"]
    hydra = by_service.get(str(service_a.id))
    standard = by_service.get(str(service_b.id))
    correct = (
        len(created) == 2
        and hydra is not None
        and standard is not None
        and hydra["patient_package_id"] == str(package.id)
        and hydra["billing_context"] == "package_prepaid"
        and standard["patient_package_id"] is None
        and standard["billing_context"] == "standard"
        and len(usage_created) == 1
        and _same_nonempty_group(created)
        and _no_money_or_pulse(db_delta(before, after))
    )
    return _make(
        scenario_id="b6_07_package_component_plus_standard_component",
        category="mixed_package_standard",
        purpose="Package entitlement must stay scoped to its eligible component inside one grouped visit.",
        turns=turns,
        before=before,
        after=after,
        verification={"package_id": str(package.id), "created": created, "package_usage_created": usage_created},
        deterministic_ok=correct,
        expected="Hydrafacial uses its package; the second service remains standard; no payment or Pulse mutation.",
        issue_severity="P1",
        issue_title="Package entitlement leaked across grouped visit components",
        atomic={
            "wrong_package_linkage": int(bool(standard and standard["patient_package_id"])),
            "wrong_entitlement_mutation": int(len(usage_created) != 1),
            "partial_grouped_writes": int(len(created) == 1),
        },
    )


def case_08_financial_handoff_blocks_grouped_write(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    before = _state(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b6_08_financial_handoff_blocks_grouped_write",
        [f"احجزيلي {service_a.name} و{service_b.name} في نفس الزيارة أقرب ميعاد، وبالمرة قولي أنا دفعت كام قبل كده"],
    )
    after = _state(db, workspace, patient)
    delta = db_delta(before, after)
    business_clean = (
        not delta["appointments"]["created"]
        and not delta["appointments"]["changed"]
        and not delta["packages"]["created"]
        and not delta["package_usages"]["created"]
        and _no_money_or_pulse(delta)
    )
    handoff = any(turn.handoff_state for turn in turns)
    ok = business_clean and handoff
    return _make(
        scenario_id="b6_08_financial_handoff_blocks_grouped_write",
        category="ownership_boundary",
        purpose="A Reception-owned ledger question combined with a grouped write must fail closed before partial booking.",
        turns=turns,
        before=before,
        after=after,
        verification={"business_delta_clean": business_clean, "handoff": handoff},
        deterministic_ok=ok,
        expected="Handoff only; zero grouped appointment/package/payment/Pulse writes.",
        issue_severity="P1",
        issue_title="Financial ownership boundary allowed a compound business write",
        atomic={"partial_grouped_writes": int(bool(delta["appointments"]["created"]))},
        global_safety={
            "financial_boundary_violations": int(not business_clean),
            "human_ownership_writes": int(bool(delta["appointments"]["created"])),
        },
        handoff_ok=handoff,
    )


def case_09_buy_package_and_book_same_service(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    before = _state(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b6_09_buy_package_and_book_same_service",
        [f"اشتريلي باقة 4 جلسات {service.name} واحجزيلي أول جلسة أقرب ميعاد"],
    )
    after = _state(db, workspace, patient)
    delta = db_delta(before, after)
    created = _created(before, after)
    before_package_ids = {row["id"] for row in before["packages"]}
    new_packages = [row for row in after["packages"] if row["id"] not in before_package_ids]
    new_package_id = new_packages[0]["id"] if len(new_packages) == 1 else None
    linked = (
        len(created) == 1
        and new_package_id is not None
        and created[0]["patient_package_id"] == new_package_id
        and created[0]["billing_context"] == "package_prepaid"
    )
    ok = (
        len(new_packages) == 1
        and linked
        and len(delta["package_usages"]["created"]) == 1
        and not delta["payments"]["created"]
        and _no_money_or_pulse(delta)
    )
    return _make(
        scenario_id="b6_09_buy_package_and_book_same_service",
        category="package_dependency",
        purpose="Package purchase must precede and ground the dependent booking without inventing a payment.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "new_packages": new_packages,
            "created": created,
            "package_usage_created": delta["package_usages"]["created"],
            "payments_created": delta["payments"]["created"],
        },
        deterministic_ok=ok,
        expected="One package is purchased, one appointment uses it, one entitlement is reserved, and no payment is recorded.",
        issue_severity="P1",
        issue_title="Package purchase dependency or entitlement linkage was incorrect",
        atomic={
            "wrong_package_linkage": int(bool(created) and not linked),
            "wrong_entitlement_mutation": int(len(delta["package_usages"]["created"]) != 1),
        },
        global_safety={"financial_boundary_violations": int(bool(delta["payments"]["created"]))},
    )


def case_10_buy_package_a_book_a_and_b(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    before = _state(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "b6_10_buy_package_a_book_a_and_b",
        [f"اشتريلي باقة 4 جلسات {service_a.name} واحجزيلي {service_a.name} و{service_b.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    after = _state(db, workspace, patient)
    delta = db_delta(before, after)
    created = _created(before, after)
    before_package_ids = {row["id"] for row in before["packages"]}
    new_packages = [row for row in after["packages"] if row["id"] not in before_package_ids]
    new_package_id = new_packages[0]["id"] if len(new_packages) == 1 else None
    by_service = {row["service_id"]: row for row in created}
    a = by_service.get(str(service_a.id))
    b = by_service.get(str(service_b.id))
    all_success = (
        len(new_packages) == 1
        and len(created) == 2
        and _same_nonempty_group(created)
        and a is not None
        and b is not None
        and a["patient_package_id"] == new_package_id
        and a["billing_context"] == "package_prepaid"
        and b["patient_package_id"] is None
        and b["billing_context"] == "standard"
        and len(delta["package_usages"]["created"]) == 1
        and not delta["payments"]["created"]
        and _no_money_or_pulse(delta)
    )
    fully_rolled_back = (
        len(new_packages) == 0
        and len(created) == 0
        and not delta["package_usages"]["created"]
        and not delta["payments"]["created"]
    )
    ok = all_success or fully_rolled_back
    partial = not all_success and not fully_rolled_back
    return _make(
        scenario_id="b6_10_buy_package_a_book_a_and_b",
        category="package_dependency",
        purpose="A package purchase plus two-service visit must keep entitlement scoped to A and remain atomic on failure.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "new_packages": new_packages,
            "created": created,
            "package_usage_created": delta["package_usages"]["created"],
            "all_success": all_success,
            "fully_rolled_back": fully_rolled_back,
        },
        deterministic_ok=ok,
        expected="Either all dependent writes succeed with package only on A, or the grouped write rolls back completely.",
        issue_severity="P1",
        issue_title="Package-dependent grouped write partially committed or leaked entitlement to another service",
        atomic={
            "partial_grouped_writes": int(partial),
            "wrong_package_linkage": int(bool(b and b["patient_package_id"])),
            "wrong_entitlement_mutation": int(bool(delta["package_usages"]["created"]) and not all_success),
        },
        global_safety={"financial_boundary_violations": int(bool(delta["payments"]["created"]))},
    )


def case_11_replace_one_service_before_commit(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    old_b = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    new_c = service_by_slug(db, workspace, "deep-facial-cleansing")
    before = _state(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_11_replace_one_service_before_commit",
        [f"احجزيلي {service_a.name} و{old_b.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    mid = _state(db, workspace, patient)
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_11_replace_one_service_before_commit",
        2,
        f"لا، بدل {old_b.name} خليها {new_c.name} وكملي الحجز",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    created = _created(before, after)
    service_ids = {row["service_id"] for row in created}
    ghost = any(row["service_id"] == str(old_b.id) for row in created)
    correct_success = (
        len(created) == 2
        and service_ids == {str(service_a.id), str(new_c.id)}
        and _same_nonempty_group(created)
        and not ghost
    )
    safe_pending = len(created) == 0 and not ghost
    partial = len(created) == 1
    ok = correct_success or safe_pending
    return _make(
        scenario_id="b6_11_replace_one_service_before_commit",
        category="compound_correction",
        purpose="Replacing one component before commit must remove the old component without disturbing the other service.",
        turns=turns,
        before=before,
        after=after,
        verification={"mid_delta": db_delta(before, mid), "created": created, "ghost_old_service": ghost},
        deterministic_ok=ok,
        expected="Final state contains A+C only, or remains safely pending with zero writes; never ghost-book B.",
        issue_severity="P1" if ghost or partial else "P2",
        issue_title="Compound service correction left a ghost or partial component",
        atomic={
            "partial_grouped_writes": int(partial),
            "wrong_component_service": int(ghost),
        },
    )


def case_12_change_device_for_one_component(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    standard = service_by_slug(db, workspace, "hydrafacial")
    laser = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    before = _state(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_12_change_device_for_one_component",
        [f"احجزيلي {standard.name} و{laser.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    mid = _state(db, workspace, patient)
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_12_change_device_for_one_component",
        2,
        "خلي الليزر على Prime Lase وكملي الحجز",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    created = _created(before, after)
    by_service = {row["service_id"]: row for row in created}
    standard_row = by_service.get(str(standard.id))
    laser_row = by_service.get(str(laser.id))
    correct_success = (
        len(created) == 2
        and standard_row is not None
        and laser_row is not None
        and standard_row["laser_device_key"] is None
        and laser_row["laser_device_key"] == "prime_lase"
        and _same_nonempty_group(created)
    )
    safe_pending = len(created) == 0
    wrong_device = bool(laser_row and laser_row["laser_device_key"] not in (None, "prime_lase"))
    partial = len(created) == 1
    ok = correct_success or safe_pending
    return _make(
        scenario_id="b6_12_change_device_for_one_component",
        category="compound_correction",
        purpose="Changing the laser device must revalidate only that component and preserve the standard component context.",
        turns=turns,
        before=before,
        after=after,
        verification={"mid_delta": db_delta(before, mid), "created": created},
        deterministic_ok=ok and not wrong_device,
        expected="Prime Lase is used only for the laser component; no stale device slot or partial group.",
        issue_severity="P1" if wrong_device or partial else "P2",
        issue_title="Component device correction reused stale context or partially committed the group",
        atomic={
            "partial_grouped_writes": int(partial),
            "wrong_component_doctor_device": int(wrong_device),
        },
    )


def case_13_side_price_query_preserves_compound(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    standard = service_by_slug(db, workspace, "hydrafacial")
    laser = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    before = _state(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_13_side_price_query_preserves_compound",
        [f"احجزيلي {standard.name} و{laser.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_13_side_price_query_preserves_compound",
        2,
        f"على فكرة سعر {laser.name} على Prime Lase كام؟",
        conversation_id,
    )
    turns.append(second)
    mid = _state(db, workspace, patient)
    _, third = send_turn(
        db,
        workspace,
        patient,
        "b6_13_side_price_query_preserves_compound",
        3,
        "تمام خليه Prime Lase وكملي الحجز",
        conversation_id,
    )
    turns.append(third)
    after = _state(db, workspace, patient)
    mid_created = _created(before, mid)
    created = _created(before, after)
    by_service = {row["service_id"]: row for row in created}
    laser_row = by_service.get(str(laser.id))
    completed = (
        len(created) == 2
        and _same_nonempty_group(created)
        and laser_row is not None
        and laser_row["laser_device_key"] == "prime_lase"
    )
    safe_pending = len(created) == 0
    ok = len(mid_created) == 0 and (completed or safe_pending)
    return _make(
        scenario_id="b6_13_side_price_query_preserves_compound",
        category="compound_continuity",
        purpose="A grounded price side query must not reset or duplicate the multi-component booking context.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "mid_created": mid_created,
            "final_created": created,
            "price_turn_reads": turns[1].verified_reads,
        },
        deterministic_ok=ok,
        expected="Price read is grounded and read-only; compound flow remains coherent and can continue without duplicate/partial writes.",
        issue_severity="P1" if len(created) == 1 else "P2",
        issue_title="Side query corrupted multi-component booking continuity",
        atomic={"partial_grouped_writes": int(len(created) == 1), "duplicate_grouped_visits": int(len(created) > 2)},
    )


def case_14_remove_one_component(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    keep = service_by_slug(db, workspace, "hydrafacial")
    remove = service_by_slug(db, workspace, "laser-hair-removal-underarm")
    before = _state(db, workspace, patient)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_14_remove_one_component",
        [f"احجزيلي {keep.name} و{remove.name} في نفس الزيارة أقرب ميعاد ورا بعض"],
    )
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_14_remove_one_component",
        2,
        f"سيبي {remove.name} وخلي {keep.name} بس وكملي",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    created = _created(before, after)
    correct = len(created) == 1 and created[0]["service_id"] == str(keep.id)
    safe_pending = len(created) == 0
    ghost = any(row["service_id"] == str(remove.id) for row in created)
    ok = (correct or safe_pending) and not ghost
    return _make(
        scenario_id="b6_14_remove_one_component",
        category="compound_correction",
        purpose="Removing one pending component must collapse the grouped intent cleanly without a ghost booking.",
        turns=turns,
        before=before,
        after=after,
        verification={"created": created, "removed_service_id": str(remove.id)},
        deterministic_ok=ok,
        expected="Exactly the retained service is booked, or the flow stays safely pending; removed service is never written.",
        issue_severity="P1" if ghost else "P2",
        issue_title="Removed compound component was still booked or flow became materially unusable",
        atomic={"wrong_component_service": int(ghost), "partial_grouped_writes": int(len(created) > 1)},
    )


def _non_joint_anchor(
    db: Session,
    workspace: Workspace,
    services: list[Service],
) -> tuple[Any, dict[str, Any]]:
    catalog = build_clinic_catalog(db, workspace)
    branch_id = active_branch_id(catalog)
    adapter = get_clinic_adapter(db=db, workspace=workspace)
    doctors = _common_doctors(catalog, services)
    today = datetime.now(UTC).date()
    for offset in range(1, 45):
        day = today + timedelta(days=offset)
        for doctor in doctors:
            first_result = adapter.get_availability(
                AvailabilityRequest(
                    branch_id=branch_id,
                    service_id=str(services[0].id),
                    booking_date=day,
                    doctor_id=str(doctor["id"]),
                )
            )
            second_result = adapter.get_availability(
                AvailabilityRequest(
                    branch_id=branch_id,
                    service_id=str(services[1].id),
                    booking_date=day,
                    doctor_id=str(doctor["id"]),
                )
            )
            second_starts = {slot.start_at for slot in second_result.slots}
            for slot in sorted(first_result.slots, key=lambda item: item.start_at, reverse=True):
                if slot.end_at not in second_starts:
                    return slot, doctor
    raise RuntimeError("EVAL_INFRA_ERROR: no first-component-only boundary anchor")


def case_15_sequence_crosses_resource_boundary(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    anchor, doctor = _non_joint_anchor(db, workspace, [service_a, service_b])
    day, time_text = _day_time(workspace, anchor)
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_15_sequence_crosses_resource_boundary",
        [f"هل ينفع {service_a.name} و{service_b.name} في نفس الزيارة يوم {day} الساعة {time_text} مع {doctor_name(doctor)}؟"],
    )
    before = _state(db, workspace, patient)
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_15_sequence_crosses_resource_boundary",
        2,
        "تمام احجزيهم في المعاد ده",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    created = _created(before, after)
    ok = len(created) == 0
    return _make(
        scenario_id="b6_15_sequence_crosses_resource_boundary",
        category="grouped_atomicity",
        purpose="A first component that fits must not commit when the complete sequential visit cannot fit.",
        turns=turns,
        before=before,
        after=after,
        verification={"anchor": anchor.start_at.isoformat(), "created": created},
        deterministic_ok=ok,
        expected="Zero partial grouped write when the complete sequence cannot fit the canonical resource window.",
        issue_severity="P1",
        issue_title="First component committed even though the complete grouped visit could not fit",
        atomic={"partial_grouped_writes": int(bool(created))},
    )


def case_16_canonical_state_changes_before_compound_commit(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service_a = service_by_slug(db, workspace, "hydrafacial")
    service_b = service_by_slug(db, workspace, "deep-facial-cleansing")
    chain, doctor = _joint_chain(db, workspace, [service_a, service_b])
    day, time_text = _day_time(workspace, chain[0])
    turns, conversation_id = _run_messages(
        db,
        workspace,
        patient,
        "b6_16_canonical_state_changes_before_compound_commit",
        [f"هل {service_a.name} و{service_b.name} متاحين في نفس الزيارة يوم {day} الساعة {time_text} مع {doctor_name(doctor)}؟"],
    )
    competitor = _new_patient(db, workspace, first_name="عميل", last_name="تغيير")
    competing = _seed_future_appointment(
        db,
        workspace,
        competitor,
        service=service_b,
        doctor_id=UUID(str(chain[1].doctor_id)),
        slot=chain[1],
    )
    before = _state(db, workspace, patient)
    _, second = send_turn(
        db,
        workspace,
        patient,
        "b6_16_canonical_state_changes_before_compound_commit",
        2,
        "تمام احجزي الزيارة دي",
        conversation_id,
    )
    turns.append(second)
    after = _state(db, workspace, patient)
    created = _created(before, after)
    fresh_reads = set(turns[-1].verified_reads)
    ok = len(created) == 0 and "availability" in fresh_reads
    return _make(
        scenario_id="b6_16_canonical_state_changes_before_compound_commit",
        category="stale_state_boundary",
        purpose="A canonical resource change between turns must force fresh verification and suppress the entire stale grouped write.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "competing_appointment_id": str(competing.id),
            "blocked_second_start": chain[1].start_at.isoformat(),
            "confirm_reads": turns[-1].verified_reads,
            "created": created,
        },
        deterministic_ok=ok,
        expected="Fresh availability validation detects the conflict and creates zero grouped components.",
        issue_severity="P1",
        issue_title="Stale grouped availability produced a partial or stale write",
        atomic={"partial_grouped_writes": int(bool(created)), "stale_grouped_writes": int(bool(created))},
    )


CASES: list[ScenarioFn] = [
    case_01_two_service_same_visit_success,
    case_02_second_component_unavailable,
    case_03_component_needs_device_clarification,
    case_04_shared_anchor_sequences_components,
    case_05_reschedule_entire_group,
    case_06_cancel_entire_standard_group,
    case_07_package_component_plus_standard_component,
    case_08_financial_handoff_blocks_grouped_write,
    case_09_buy_package_and_book_same_service,
    case_10_buy_package_a_book_a_and_b,
    case_11_replace_one_service_before_commit,
    case_12_change_device_for_one_component,
    case_13_side_price_query_preserves_compound,
    case_14_remove_one_component,
    case_15_sequence_crosses_resource_boundary,
    case_16_canonical_state_changes_before_compound_commit,
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
        acquire_eval_advisory_lock(db, namespace="tia-agent-eval-batch-06")
        workspace = db.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
        if workspace is None:
            raise RuntimeError("Workspace not found")
        assert_demo_only(workspace)
        return case_fn(db, workspace)
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult(
            id=f"b6_{case_fn.__name__.removeprefix('case_')}",
            category="infrastructure",
            purpose="Scenario execution failed before review.",
            turns=[],
            state_before={},
            state_after={},
            db_verification={
                "atomicity_counters": _zero_flags(_ATOMIC_KEYS),
                "global_safety_counters": _zero_flags(_GLOBAL_KEYS),
            },
            evaluation=default_evaluation(db_ok=False, grounding_ok=False),
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
                "root_cause": "Infrastructure/provider noise or test-data problem",
            },
        )
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def _counter_summary(
    results: list[ScenarioResult],
    key: str,
    names: tuple[str, ...],
) -> dict[str, int]:
    return {
        name: sum(
            int((row.db_verification.get(key) or {}).get(name) or 0)
            for row in results
        )
        for name in names
    }


def _batch_summary(results: list[ScenarioResult]) -> dict[str, Any]:
    summary = summarize(results)
    summary["atomicity_counters"] = _counter_summary(
        results,
        "atomicity_counters",
        _ATOMIC_KEYS,
    )
    summary["global_safety_counters"] = _counter_summary(
        results,
        "global_safety_counters",
        _GLOBAL_KEYS,
    )
    return summary


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

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == ns.workspace_slug))
        if workspace is None:
            raise RuntimeError("Demo workspace not found.")
        preflight = _preflight(db, workspace)
        demo_seed = {
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "history_loader_limit": settings.agent_history_messages,
            "fixture_version": BATCH6_FIXTURE_VERSION,
            **preflight,
        }

    results: list[ScenarioResult] = []
    stopped_for_p0 = False
    for case_fn in CASES:
        row = run_case(engine, ns.workspace_slug, case_fn)
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

    summary = _batch_summary(results)
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
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_metadata": {
            "batch": BATCH_NUMBER,
            "scenario_version": SCENARIO_VERSION,
            "git_sha": ns.git_sha,
            "batch6_base_sha": ns.batch6_base_sha,
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
    json_path = output_dir / f"batch_06_raw_{timestamp}.json"
    md_path = output_dir / f"batch_06_raw_{timestamp}.md"
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
    print(
        "ATOMICITY_COUNTERS="
        + json.dumps(summary["atomicity_counters"], ensure_ascii=False, separators=(",", ":"))
    )
    print(
        "GLOBAL_SAFETY_COUNTERS="
        + json.dumps(summary["global_safety_counters"], ensure_ascii=False, separators=(",", ":"))
    )
    engine.dispose()
    return 2 if stopped_for_p0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
