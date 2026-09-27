from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.workspace import Workspace
from app.services.package_offers import list_package_offers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.agent_eval.harness import (
    ScenarioResult,
    acquire_eval_advisory_lock,
    assert_demo_only,
    booking_context,
    default_evaluation,
    jsonable,
    local_slot,
    money,
    service_by_slug,
)
from tools.agent_eval.run_batch_01 import (
    context_with_two_doctors,
    doctor_name,
    quiet_patient,
)
from tools.agent_eval.run_batch_02 import (
    _apply_cost,
    _ensure_batch2_catalog_fixtures,
    _seed_historical_appointment,
    laser_context,
)
from tools.agent_eval.run_batch_03 import (
    _future_slot,
    _run_messages,
    _seed_future_appointment,
    _seed_package_balance,
    db_delta,
    extended_state_snapshot,
    make_result,
    summarize,
)

ScenarioFn = Callable[[Session, Workspace], ScenarioResult]
BATCH_LABEL = "08-10-combined"
SCENARIO_VERSION = "combined-08-10-v1"
FIXTURE_VERSION = "combined-08-10-demo-fixtures-v1"

STALE_COUNTER_KEYS = (
    "stale_service_carryovers",
    "stale_doctor_carryovers",
    "stale_device_carryovers",
    "stale_date_time_carryovers",
    "stale_package_carryovers",
    "wrong_active_task_target",
    "unexpected_task_restart",
    "unexpected_task_loss",
    "duplicate_writes",
    "stale_lifecycle_writes",
    "wrong_appointment_writes",
    "side_read_business_writes",
    "financial_boundary_violations",
    "human_ownership_writes",
    "invented_entity_writes",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument("--output-dir", default="backend/eval_results")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA") or "unknown")
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--input-price-per-million", type=float, required=True)
    parser.add_argument("--cached-input-price-per-million", type=float, required=True)
    parser.add_argument("--output-price-per-million", type=float, required=True)
    parser.add_argument("--cache-write-multiplier", type=float, default=1.0)
    parser.add_argument("--pricing-source", required=True)
    return parser.parse_args()


def require_explicit_demo_eval() -> None:
    if os.getenv("TIA_AGENT_EVAL_CONFIRM_DEMO") != "1":
        raise RuntimeError("Set TIA_AGENT_EVAL_CONFIRM_DEMO=1 to run the live Demo evaluation.")


def _counter(**values: int) -> dict[str, int]:
    row = {key: 0 for key in STALE_COUNTER_KEYS}
    for key, value in values.items():
        if key not in row:
            raise KeyError(key)
        row[key] = int(value)
    return row


def _business_delta_clean(delta: dict[str, Any], *, allow_handoff: bool = False) -> bool:
    for key in (
        "appointments",
        "packages",
        "package_usages",
        "pulse_packs",
        "payments",
        "pulse_usages",
        "pulse_settlements",
    ):
        row = delta.get(key) or {}
        if row.get("created") or row.get("removed") or row.get("changed"):
            return False
    if delta.get("pulse_balance_delta"):
        return False
    if not allow_handoff:
        row = delta.get("handoffs") or {}
        if row.get("created") or row.get("removed") or row.get("changed"):
            return False
    return True


def _responses(turns) -> list[str]:
    return [str(turn.agent_response or "") for turn in turns]


def _contains_all(text: str, values: list[str]) -> bool:
    lowered = text.casefold()
    return all(str(value).casefold() in lowered for value in values if str(value).strip())


def _contains_any(text: str, values: list[str]) -> bool:
    lowered = text.casefold()
    return any(str(value).casefold() in lowered for value in values if str(value).strip())


def _read_case(
    *,
    db: Session,
    workspace: Workspace,
    scenario_id: str,
    lane: str,
    purpose: str,
    risk: str,
    patient,
    messages: list[str],
    required_all: list[str] | None = None,
    required_any: list[str] | None = None,
    require_verified_read: bool = True,
    allow_handoff: bool = False,
    issue_severity: str = "P2",
) -> ScenarioResult:
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(db, workspace, patient, scenario_id, messages)
    after = extended_state_snapshot(db, workspace, patient)
    delta = db_delta(before, after)
    clean = _business_delta_clean(delta, allow_handoff=allow_handoff)
    reads_ok = any(turn.verified_reads for turn in turns) if require_verified_read else True
    final = _responses(turns)[-1] if turns else ""
    all_ok = _contains_all(final, required_all or [])
    any_ok = _contains_any(final, required_any or []) if required_any else True
    deterministic_ok = clean and reads_ok and all_ok and any_ok
    result = make_result(
        scenario_id=scenario_id,
        category=lane,
        purpose=purpose,
        turns=turns,
        before=before,
        after=after,
        verification={
            "lane": lane,
            "risk": risk,
            "business_state_unchanged": clean,
            "verified_read_observed": reads_ok,
            "required_all": required_all or [],
            "required_any": required_any or [],
            "final_response": final,
        },
        deterministic_ok=deterministic_ok,
        expected=(
            f"{purpose} Business state must remain unchanged; factual claims must be grounded "
            "in verified reads where applicable."
        ),
        issue_severity=issue_severity,
        issue_title="Combined Batch read/recovery invariant mismatch",
        issue_detail=f"lane={lane}; risk={risk}; clean={clean}; reads_ok={reads_ok}",
    )
    result.db_verification["state_continuity_counters"] = _counter(
        side_read_business_writes=0 if clean else 1,
        financial_boundary_violations=1 if "financial" in risk.casefold() and not clean else 0,
    )
    return result


def case_01_service_catalog_discovery(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    catalog = build_clinic_catalog(db, workspace)
    names = [
        str(row.get("name"))
        for row in catalog.get("services", [])
        if isinstance(row, dict) and row.get("name") and row.get("is_active", True)
    ][:5]
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a01_service_catalog_discovery",
        lane="A_breadth_grounded_reads",
        purpose="Discover active clinic services without starting a booking.",
        risk="invented service list or accidental booking",
        patient=patient,
        messages=["إيه الخدمات اللي عندكم؟"],
        required_any=names,
    )


def case_02_service_price_duration(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a02_service_price_duration",
        lane="A_breadth_grounded_reads",
        purpose="Answer exact current service price and duration.",
        risk="stale price or invented duration",
        patient=patient,
        messages=["جلسة Hydrafacial بكام وبتاخد قد إيه؟"],
        required_all=[money(service.price_minor), str(service.duration_minutes)],
    )


def case_03_doctors_for_service(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, first, second = context_with_two_doctors(db, workspace)
    names = [doctor_name(first[0]), doctor_name(second[0])]
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a03_doctors_for_service",
        lane="A_breadth_grounded_reads",
        purpose="List compatible doctors for a service from current catalog data.",
        risk="invented or incompatible doctor",
        patient=patient,
        messages=[f"مين الدكاترة اللي بيعملوا {service['name']}؟"],
        required_any=names,
    )


def case_04_availability_read_only(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, doctor, _, _, available = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    date_text, time_text = local_slot(available, available.slots[0])
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a04_availability_read_only",
        lane="A_breadth_grounded_reads",
        purpose="Read exact availability without creating a booking.",
        risk="availability hallucination or implicit booking",
        patient=patient,
        messages=[f"مواعيد {service['name']} مع {doctor_name(doctor)} يوم {date_text} إيه؟"],
        required_any=[time_text, doctor_name(doctor)],
    )


def case_05_device_price_comparison(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service1, _, _, slot1 = laser_context(db, workspace, device_key="prime_lase")
    _, _, _, _, slot2 = laser_context(db, workspace, device_key="candela_gentle")
    name1 = str(slot1.laser_device_name or "Prime Lase")
    name2 = str(slot2.laser_device_name or "Candela Gentle")
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a05_device_price_comparison",
        lane="A_breadth_grounded_reads",
        purpose="Compare laser device-specific current prices without unsupported quality claims.",
        risk="device-price binding or invented comparison claims",
        patient=patient,
        messages=[f"{service1['name']} على {name1} وعلى {name2} بكام؟"],
        required_any=[money(slot1.price_minor), money(slot2.price_minor), name1, name2],
    )


def case_06_package_offers_read(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    offers = list_package_offers(db, workspace_id=workspace.id, active_only=True)
    names = [str(getattr(row, "name", "") or "") for row in offers][:5]
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a06_package_offers_read",
        lane="A_breadth_grounded_reads",
        purpose="Read current package offers without purchasing or booking.",
        risk="invented offer or package purchase write",
        patient=patient,
        messages=["إيه الباكدجات المتاحة دلوقتي؟"],
        required_any=[name for name in names if name] or None,
    )


def case_07_package_remaining_read(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _seed_package_balance(
        db,
        workspace,
        patient,
        service,
        remaining=2,
        name="Combined Eval Hydrafacial Package",
    )
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a07_package_remaining_read",
        lane="A_breadth_grounded_reads",
        purpose="Read remaining sessions from an owned session package.",
        risk="wrong remaining-session count or package mutation",
        patient=patient,
        messages=["فاضلي كام جلسة في الباكدج بتاعتي؟"],
        required_any=["2", "جلستين", "جلستان"],
    )


def case_08_pulse_reads_combined(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a08_pulse_reads_combined",
        lane="A_breadth_grounded_reads",
        purpose="Read Pulse balance, owned packs, and offers without checkout semantics.",
        risk="financial boundary violation or invented Pulse ownership",
        patient=patient,
        messages=["رصيدي كام Pulse؟ وعندي باقات Pulse إيه والعروض المتاحة إيه؟"],
        allow_handoff=True,
    )


def case_09_upcoming_appointments_read(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _, service_row, doctor, _, _, available = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    slot = available.slots[0]
    _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        slot=slot,
    )
    date_text, _ = local_slot(available, slot)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a09_upcoming_appointments_read",
        lane="A_breadth_grounded_reads",
        purpose="Read the patient's upcoming appointment from canonical DB state.",
        risk="wrong patient appointment or stale lifecycle state",
        patient=patient,
        messages=["مواعيدي الجاية إيه؟"],
        required_any=[str(service_row["name"]), date_text],
    )


def case_10_appointment_history_read(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _, _, doctor, branch_id, _, _ = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    _seed_historical_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        branch_id=UUID(str(branch_id)),
        days_ago=20,
        status="completed",
    )
    _seed_historical_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        branch_id=UUID(str(branch_id)),
        days_ago=10,
        status="cancelled",
    )
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_a10_appointment_history_read",
        lane="A_breadth_grounded_reads",
        purpose="Read completed/cancelled appointment history without lifecycle writes.",
        risk="historical read mutates lifecycle or invents appointment",
        patient=patient,
        messages=["وريني آخر مواعيدي واللي اتلغى منها."],
        required_any=[service.name],
    )


def case_11_vague_service_clarification(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b01_vague_service_clarification",
        lane="B_ambiguity_recovery_corrections",
        purpose="Clarify a vague service request instead of guessing an entity.",
        risk="invented entity or automatic selection",
        patient=patient,
        messages=["عايزة جلسة بشرة."],
        require_verified_read=False,
    )


def case_12_option_followup_second_doctor(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, first, second = context_with_two_doctors(db, workspace)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b02_option_followup_second_doctor",
        lane="B_ambiguity_recovery_corrections",
        purpose="Resolve a natural option follow-up from the immediately verified doctor list.",
        risk="option-reference drift or invented doctor",
        patient=patient,
        messages=[
            f"مين الدكاترة اللي بيعملوا {service['name']}؟",
            "طب والدكتورة التانية مواعيدها إيه؟",
        ],
        required_any=[doctor_name(first[0]), doctor_name(second[0])],
    )


def case_13_service_correction_read(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    prp = service_by_slug(db, workspace, "prp-skin")
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b03_service_correction_read",
        lane="B_ambiguity_recovery_corrections",
        purpose="Honor the latest service correction in a read-only price flow.",
        risk="stale service carryover",
        patient=patient,
        messages=["Hydrafacial بكام؟", "لا قصدي PRP للبشرة."],
        required_all=[money(prp.price_minor)],
    )


def case_14_date_correction_availability(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _, _, doctor, _, _, available1 = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    slot1 = available1.slots[0]
    available2, slot2 = _future_slot(
        db,
        workspace,
        service_id=str(service.id),
        doctor_id=str(doctor["id"]),
        after_date=slot1.start_at.date(),
    )
    date1, _ = local_slot(available1, slot1)
    date2, time2 = local_slot(available2, slot2)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b04_date_correction_availability",
        lane="B_ambiguity_recovery_corrections",
        purpose="Use the corrected date for availability and drop the stale date.",
        risk="stale date/time carryover",
        patient=patient,
        messages=[
            f"مواعيد Hydrafacial مع {doctor_name(doctor)} يوم {date1}؟",
            f"لا قصدي يوم {date2}.",
        ],
        required_any=[time2, date2],
    )


def case_15_device_correction_read(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, _, _, _, candela_slot = laser_context(db, workspace, device_key="candela_gentle")
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b05_device_correction_read",
        lane="B_ambiguity_recovery_corrections",
        purpose="Honor a device correction in a laser price read.",
        risk="stale device-price binding",
        patient=patient,
        messages=["ليزر الإبط على Prime Lase بكام؟", "لا قصدي Candela Gentle."],
        required_all=[money(candela_slot.price_minor)],
    )


def case_16_fragmented_egyptian_arabic(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b06_fragmented_egyptian_arabic",
        lane="B_ambiguity_recovery_corrections",
        purpose="Recover a fragmented Egyptian-Arabic laser intent without premature booking.",
        risk="premature write or context loss across fragments",
        patient=patient,
        messages=["عايزة ليزر", "إبط", "كانديلا", "بكرة ينفع؟"],
        require_verified_read=False,
    )


def case_17_mixed_arabic_english(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, doctor, _, _, available = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    date_text, _ = local_slot(available, available.slots[0])
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b07_mixed_arabic_english",
        lane="B_ambiguity_recovery_corrections",
        purpose="Understand mixed Arabic/English availability intent without an implicit booking.",
        risk="semantic loss from code-switching",
        patient=patient,
        messages=[
            f"عايزة check availability for {service['name']} with {doctor_name(doctor)} يوم {date_text}"
        ],
        required_any=[doctor_name(doctor), date_text],
    )


def case_18_unsupported_then_recover(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_b08_unsupported_then_recover",
        lane="B_ambiguity_recovery_corrections",
        purpose="Fail safely on an unsupported clinic-specific fact, then recover to a grounded read.",
        risk="invented clinic fact poisoning later context",
        patient=patient,
        messages=["عندكم parking جوه العيادة؟", "طيب Hydrafacial بكام؟"],
        required_all=[money(service.price_minor)],
        allow_handoff=True,
    )

def case_19_repeated_price_consistency(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "c0810_c01_repeated_price_consistency",
        ["Hydrafacial بكام؟", "معلش أكدلي السعر تاني."],
    )
    after = extended_state_snapshot(db, workspace, patient)
    delta = db_delta(before, after)
    price = money(service.price_minor)
    responses = _responses(turns)
    clean = _business_delta_clean(delta)
    ok = clean and all(price in response.replace(",", "") for response in responses)
    result = make_result(
        scenario_id="c0810_c01_repeated_price_consistency",
        category="C_long_tail_quality",
        purpose="Repeat the same grounded read consistently without writes.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "responses": responses,
            "expected_price": price,
            "business_state_unchanged": clean,
        },
        deterministic_ok=ok,
        expected="Both answers use the same current DB price and no business state changes.",
        issue_severity="P2",
        issue_title="Repeated grounded read became inconsistent",
        issue_detail=f"expected_price={price}",
    )
    result.db_verification["state_continuity_counters"] = _counter(
        side_read_business_writes=0 if clean else 1,
    )
    return result


def case_20_detour_then_resume_doctors(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    _, service, _, first, second = context_with_two_doctors(db, workspace)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_c02_detour_then_resume_doctors",
        lane="C_long_tail_quality",
        purpose="Resume a grounded service read after a harmless unsupported detour.",
        risk="detour corrupts service reference",
        patient=patient,
        messages=[
            f"{service['name']} بكام؟",
            "بالمناسبة عندكم واي فاي للناس اللي مستنية؟",
            f"نرجع لـ {service['name']}، مين الدكاترة؟",
        ],
        required_any=[doctor_name(first[0]), doctor_name(second[0])],
        allow_handoff=True,
    )


def case_21_pronoun_reference_duration(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_c03_pronoun_reference_duration",
        lane="C_long_tail_quality",
        purpose="Resolve a natural pronoun/reference follow-up to the verified service.",
        risk="reference loss or unrelated service carryover",
        patient=patient,
        messages=["Hydrafacial بكام؟", "وطب مدتها قد إيه؟"],
        required_all=[str(service.duration_minutes)],
    )


def case_22_negative_booking_correction(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_c04_negative_booking_correction",
        lane="C_long_tail_quality",
        purpose="Respect a correction from booking intent to information-only intent.",
        risk="booking write after explicit withdrawal",
        patient=patient,
        messages=["عايزة أحجز Hydrafacial.", "لا مش عايزة أحجز، بس بسأل على السعر."],
        required_all=[money(service.price_minor)],
    )


def case_23_explicit_human_handoff(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "c0810_c05_explicit_human_handoff",
        ["ممكن أكلم الريسبشن؟"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    delta = db_delta(before, after)
    clean = _business_delta_clean(delta, allow_handoff=True)
    handoff_delta = delta.get("handoffs") or {}
    handoff_changed = bool(handoff_delta.get("created") or handoff_delta.get("changed"))
    ok = clean and handoff_changed
    result = make_result(
        scenario_id="c0810_c05_explicit_human_handoff",
        category="C_long_tail_quality",
        purpose="Honor explicit human handoff without unrelated business writes.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "handoff_created_or_changed": handoff_changed,
            "business_state_unchanged": clean,
        },
        deterministic_ok=ok,
        expected="Create/activate the handoff and do not mutate appointments, packages, Pulse, or payments.",
        issue_severity="P1",
        issue_title="Explicit human handoff boundary mismatch",
        issue_detail=f"handoff_changed={handoff_changed}; clean={clean}",
        handoff_ok=handoff_changed,
    )
    result.db_verification["state_continuity_counters"] = _counter(
        human_ownership_writes=0 if clean else 1,
    )
    return result


def case_24_financial_boundary_read(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    before = extended_state_snapshot(db, workspace, patient)
    turns, _ = _run_messages(
        db,
        workspace,
        patient,
        "c0810_c06_financial_boundary_read",
        ["عندي Pulse كفاية؟ ولو حجزت هيتخصموا ولا أدفع كاش؟"],
    )
    after = extended_state_snapshot(db, workspace, patient)
    delta = db_delta(before, after)
    clean = _business_delta_clean(delta, allow_handoff=True)
    final = _responses(turns)[-1] if turns else ""
    boundary_ack = _contains_any(final, ["الريسبشن", "Reception", "الاستقبال"])
    ok = clean and boundary_ack
    result = make_result(
        scenario_id="c0810_c06_financial_boundary_read",
        category="C_long_tail_quality",
        purpose="Answer safe Pulse facts while keeping cash/Pulse settlement with Reception.",
        turns=turns,
        before=before,
        after=after,
        verification={
            "business_state_unchanged": clean,
            "reception_boundary_acknowledged": boundary_ack,
            "final_response": final,
        },
        deterministic_ok=ok,
        expected="Safe Pulse reads may continue, but cash-vs-Pulse settlement remains Reception-owned.",
        issue_severity="P1",
        issue_title="Pulse financial ownership boundary mismatch",
        issue_detail=f"clean={clean}; boundary_ack={boundary_ack}",
    )
    result.db_verification["state_continuity_counters"] = _counter(
        financial_boundary_violations=0 if clean and boundary_ack else 1,
    )
    return result


def case_25_long_detour_resume_appointment(db: Session, workspace: Workspace) -> ScenarioResult:
    patient = quiet_patient(db, workspace)
    service = service_by_slug(db, workspace, "hydrafacial")
    _, service_row, doctor, _, _, available = booking_context(
        db, workspace, service_slug="hydrafacial"
    )
    slot = available.slots[0]
    _seed_future_appointment(
        db,
        workspace,
        patient,
        service=service,
        doctor_id=UUID(str(doctor["id"])),
        slot=slot,
    )
    date_text, _ = local_slot(available, slot)
    return _read_case(
        db=db,
        workspace=workspace,
        scenario_id="c0810_c07_long_detour_resume_appointment",
        lane="C_long_tail_quality",
        purpose="Return to the same canonical upcoming appointment after harmless informational detours.",
        risk="patient/history context loss or lifecycle write",
        patient=patient,
        messages=[
            "معادي الجاي إمتى؟",
            "Hydrafacial بكام؟",
            "في عروض Pulse إيه؟",
            "مين الدكاترة اللي بيعملوا Hydrafacial؟",
            "طيب معادي اللي جاي إمتى بالظبط؟",
        ],
        required_any=[str(service_row["name"]), date_text],
        allow_handoff=True,
    )


CASES: list[ScenarioFn] = [
    case_01_service_catalog_discovery,
    case_02_service_price_duration,
    case_03_doctors_for_service,
    case_04_availability_read_only,
    case_05_device_price_comparison,
    case_06_package_offers_read,
    case_07_package_remaining_read,
    case_08_pulse_reads_combined,
    case_09_upcoming_appointments_read,
    case_10_appointment_history_read,
    case_11_vague_service_clarification,
    case_12_option_followup_second_doctor,
    case_13_service_correction_read,
    case_14_date_correction_availability,
    case_15_device_correction_read,
    case_16_fragmented_egyptian_arabic,
    case_17_mixed_arabic_english,
    case_18_unsupported_then_recover,
    case_19_repeated_price_consistency,
    case_20_detour_then_resume_doctors,
    case_21_pronoun_reference_duration,
    case_22_negative_booking_correction,
    case_23_explicit_human_handoff,
    case_24_financial_boundary_read,
    case_25_long_detour_resume_appointment,
]


def _run_case(engine, workspace_slug: str, case_fn: ScenarioFn) -> ScenarioResult:
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        acquire_eval_advisory_lock(db, namespace="tia-agent-eval-combined-08-10")
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
            db_verification={"state_continuity_counters": _counter()},
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
                "root_cause": "Infrastructure/provider noise or evaluation fixture problem",
            },
        )
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


def _state_counter_summary(results: list[ScenarioResult]) -> dict[str, int]:
    totals = {key: 0 for key in STALE_COUNTER_KEYS}
    for result in results:
        counters = result.db_verification.get("state_continuity_counters") or {}
        for key in totals:
            totals[key] += int(counters.get(key) or 0)
    return totals


def _write_reports(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Tia Agent Evaluation — Combined Batch 08–10 Raw Baseline",
        "",
        f"- Runtime base SHA: {payload['run_metadata']['base_sha']}",
        f"- Harness SHA: {payload['run_metadata']['git_sha']}",
        f"- Scenario version: {payload['run_metadata']['scenario_version']}",
        f"- Fixture version: {payload['run_metadata']['demo_seed']['fixture_version']}",
        f"- Model: {payload['run_metadata']['model']}",
        f"- Reasoning: {payload['run_metadata']['reasoning_effort']}",
        f"- Scenarios executed: {len(payload['scenario_results'])}",
        "",
        "Automated findings are advisory. Final classification requires manual trace/DB adjudication.",
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
            lines.append(f"Tia: {turn['agent_response']}")
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
        lines.append("State / DB verification:")
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
        assert_demo_only(workspace)
        _ensure_batch2_catalog_fixtures(db, workspace)
        catalog = build_clinic_catalog(db, workspace)
        demo_seed = {
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "fixture_version": FIXTURE_VERSION,
            "history_loader_limit": settings.agent_history_messages,
            "active_branch_count": len([
                row
                for row in catalog.get("branches", [])
                if isinstance(row, dict) and row.get("is_active", True)
            ]),
            "service_count": len([
                row
                for row in catalog.get("services", [])
                if isinstance(row, dict) and row.get("is_active", True)
            ]),
            "doctor_count": len([
                row
                for row in catalog.get("doctors", [])
                if isinstance(row, dict) and row.get("id")
            ]),
            "package_offer_count": len(
                list_package_offers(db, workspace_id=workspace.id, active_only=True)
            ),
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
    state_counters = _state_counter_summary(results)
    total_actual_cost = sum(float(row.cost.get("actual_total_usd") or 0) for row in results)
    total_without_cache = sum(
        float(row.cost.get("without_explicit_cache_usd") or 0)
        for row in results
    )
    total_turns = int(summary.get("total_turns") or 0)
    total_calls = int(summary.get("total_llm_calls") or 0)
    total_tokens = int(summary.get("tokens", {}).get("total_tokens") or 0)
    summary["state_continuity_counters"] = state_counters
    summary["average_turns_per_scenario"] = (
        round(total_turns / len(results), 2) if results else 0.0
    )
    summary["tokens_per_customer_turn"] = (
        round(total_tokens / total_turns, 2) if total_turns else 0.0
    )
    summary["llm_calls_per_customer_turn"] = (
        round(total_calls / total_turns, 3) if total_turns else 0.0
    )
    summary["provider_latency_ms"] = int(
        summary.get("stage_metrics", {}).get("all", {}).get("latency_ms") or 0
    )
    summary["e2e_latency_ms"] = sum(
        int(turn.latency_ms) for row in results for turn in row.turns
    )
    summary["retries"] = sum(
        int(summary.get("stage_metrics", {}).get(stage, {}).get("retries") or 0)
        for stage in ("interpreter", "responder")
    )
    summary["fallbacks"] = sum(
        int(summary.get("stage_metrics", {}).get(stage, {}).get("fallback_calls") or 0)
        for stage in ("interpreter", "responder")
    )
    summary["cost"] = {
        "actual_usd": round(total_actual_cost, 8),
        "without_explicit_cache_usd": round(total_without_cache, 8),
        "saving_usd": round(max(0.0, total_without_cache - total_actual_cost), 8),
        "saving_percent": round(
            (
                (total_without_cache - total_actual_cost)
                / total_without_cache
                * 100.0
            )
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
            "batch": BATCH_LABEL,
            "scenario_version": SCENARIO_VERSION,
            "git_sha": ns.git_sha,
            "base_sha": ns.base_sha,
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
            "isolation": {
                "demo_guard": True,
                "advisory_lock": "tia-agent-eval-combined-08-10",
                "rollback_per_scenario": True,
            },
        },
        "scenario_results": [jsonable(row) for row in results],
        "batch_summary": summary,
    }
    json_path = output_dir / f"batch_08_10_combined_raw_{timestamp}.json"
    md_path = output_dir / f"batch_08_10_combined_raw_{timestamp}.md"
    _write_reports(payload, json_path, md_path)

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
    print(f"TOKENS_PER_TURN={summary['tokens_per_customer_turn']}")
    print(f"LLM_CALLS_PER_TURN={summary['llm_calls_per_customer_turn']}")
    print(f"ACTUAL_COST_USD={summary['cost']['actual_usd']}")
    print(f"WITHOUT_CACHE_USD={summary['cost']['without_explicit_cache_usd']}")
    print(f"CACHE_SAVING_PERCENT={summary['cost']['saving_percent']}")
    print(
        "STATE_CONTINUITY_COUNTERS="
        + json.dumps(state_counters, separators=(",", ":"))
    )
    return 2 if stopped_for_p0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
