from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

import app.agents.v2.time_resolution as time_resolution
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from scripts import run_explicit_daypart_live_review as base

REPORT_PATH = Path(
    os.environ.get(
        "CLOCK_RT_REPORT_PATH",
        "artifacts/clinic-hours-clock-live-review.json",
    )
)

SAFETY_COUNTERS = {
    "wrong_time_writes": 0,
    "wrong_writes": 0,
    "false_success": 0,
    "silent_am_pm_mutation": 0,
    "unnecessary_10_11_clarification": 0,
    "arbitrary_10_11_resolution": 0,
    "contextual_period_misresolution": 0,
    "wrong_device": 0,
    "cross_device_visits": 0,
    "lost_services": 0,
    "duplicate_appointments": 0,
    "duplicate_proposals": 0,
    "persisted_audit_writes": 0,
}

CLOCK_TRACE: list[dict[str, Any]] = []
_ORIGINAL_RESOLVE_BARE = time_resolution.resolve_bare_clock_constraint


def _capture_bare_resolution(*args, **kwargs):
    result = _ORIGINAL_RESOLVE_BARE(*args, **kwargs)
    constraint = args[0] if args else kwargs.get("constraint")
    date_constraint = kwargs.get("date_constraint")
    CLOCK_TRACE.append(
        {
            "input_time": base._dump(constraint),
            "date_constraint": base._dump(date_constraint),
            "branch_id": kwargs.get("branch_id"),
            "status": result.status,
            "candidate_clocks": list(result.candidate_clocks),
            "valid_operating_hour_candidates": list(
                result.valid_operating_hour_candidates
            ),
            "operating_hours": [
                {"weekday": weekday, "start": start, "end": end}
                for weekday, start, end in result.operating_hours
            ],
            "canonical_time": (
                result.constraint.start_time
                if result.constraint is not None
                else None
            ),
            "requested_date": result.requested_date,
        }
    )
    return result


time_resolution.resolve_bare_clock_constraint = _capture_bare_resolution


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    customer: str
    start: str
    end: str
    expected_source: str
    expected_period: str
    expected_canonical: str | None
    expected_bare_status: str | None = None
    expected_candidates: tuple[str, ...] = ()
    expected_valid_candidates: tuple[str, ...] = ()
    clarification_required: bool = False
    unavailable: bool = False


SCENARIOS = [
    ScenarioSpec(
        name="RT1",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3",
        start="10:00",
        end="22:00",
        expected_source="bare_spoken_12h",
        expected_period="none",
        expected_canonical="15:00",
        expected_bare_status="resolved",
        expected_candidates=("03:00", "15:00"),
        expected_valid_candidates=("15:00",),
    ),
    ScenarioSpec(
        name="RT2",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 10",
        start="09:00",
        end="21:00",
        expected_source="bare_spoken_12h",
        expected_period="none",
        expected_canonical="10:00",
        expected_bare_status="resolved",
        expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00",),
    ),
    ScenarioSpec(
        name="RT3",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 10",
        start="09:00",
        end="23:30",
        expected_source="bare_spoken_12h",
        expected_period="none",
        expected_canonical=None,
        expected_bare_status="clarify_period",
        expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"),
        clarification_required=True,
    ),
    ScenarioSpec(
        name="RT4",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11",
        start="09:00",
        end="22:00",
        expected_source="bare_spoken_12h",
        expected_period="none",
        expected_canonical="11:00",
        expected_bare_status="resolved",
        expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("11:00",),
    ),
    ScenarioSpec(
        name="RT5",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11",
        start="14:00",
        end="23:30",
        expected_source="bare_spoken_12h",
        expected_period="none",
        expected_canonical="23:00",
        expected_bare_status="resolved",
        expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("23:00",),
    ),
    ScenarioSpec(
        name="RT6",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 12",
        start="10:00",
        end="22:00",
        expected_source="bare_spoken_12h",
        expected_period="none",
        expected_canonical="12:00",
        expected_bare_status="resolved",
        expected_candidates=("12:00",),
        expected_valid_candidates=("12:00",),
    ),
    ScenarioSpec(
        name="RT7",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 03:00 بالضبط",
        start="10:00",
        end="22:00",
        expected_source="literal_24h",
        expected_period="none",
        expected_canonical="03:00",
        unavailable=True,
    ),
    ScenarioSpec(
        name="RT8",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4:15 العصر بالضبط",
        start="10:00",
        end="22:00",
        expected_source="explicit_period",
        expected_period="explicit_pm",
        expected_canonical="16:15",
        unavailable=True,
    ),
    ScenarioSpec(
        name="RT9",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11 بالليل بالضبط",
        start="09:00",
        end="21:00",
        expected_source="explicit_period",
        expected_period="explicit_pm",
        expected_canonical="23:00",
        unavailable=True,
    ),
]


def _require(condition: bool, *, counter: str | None = None, message: str) -> None:
    if condition:
        return
    if counter is not None:
        SAFETY_COUNTERS[counter] += 1
    raise AssertionError(message)


def _set_operating_hours(ctx: base.Context, *, start: str, end: str) -> None:
    start_time = time.fromisoformat(start)
    end_time = time.fromisoformat(end)
    branch_rows = list(
        ctx.db.scalars(
            select(BranchWorkingHour).where(
                BranchWorkingHour.workspace_id == ctx.workspace.id,
                BranchWorkingHour.branch_id == ctx.workspace.primary_branch_id,
            )
        )
    )
    doctor_rows = list(
        ctx.db.scalars(
            select(DoctorWorkingHour).where(
                DoctorWorkingHour.workspace_id == ctx.workspace.id,
                DoctorWorkingHour.doctor_id == ctx.doctor.id,
                DoctorWorkingHour.branch_id == ctx.workspace.primary_branch_id,
            )
        )
    )
    assert len(branch_rows) == 7
    assert len(doctor_rows) == 7
    for row in [*branch_rows, *doctor_rows]:
        row.start_time = start_time
        row.end_time = end_time
    ctx.db.commit()


def _semantic_operation(record: dict[str, Any]) -> dict[str, Any]:
    for payload in reversed(record.get("raw_structured") or []):
        if not isinstance(payload, dict):
            continue
        operations = payload.get("operations")
        if not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            entities = operation.get("entities")
            time_value = entities.get("time") if isinstance(entities, dict) else None
            if isinstance(time_value, dict) and time_value.get("start_time") not in (None, ""):
                return operation
    return {}


def _semantic_time(operation: dict[str, Any]) -> dict[str, Any]:
    entities = operation.get("entities")
    value = entities.get("time") if isinstance(entities, dict) else None
    return value if isinstance(value, dict) else {}


def _original_bare_trace() -> dict[str, Any] | None:
    for item in CLOCK_TRACE:
        raw = item.get("input_time")
        if not isinstance(raw, dict):
            continue
        if raw.get("start_time_ambiguity") == "twelve_hour":
            return item
    return None


def _availability_clock(record: dict[str, Any]) -> str | None:
    for request in record.get("availability_requests") or []:
        if not isinstance(request, dict):
            continue
        time_value = request.get("time")
        if isinstance(time_value, dict) and time_value.get("start_time"):
            return str(time_value["start_time"])
    return None


def _proposal_clock(record: dict[str, Any]) -> str | None:
    proposal = base._proposal(record) or record.get("booking_proposal")
    if not isinstance(proposal, dict):
        return None
    start = proposal.get("start_at")
    if not start:
        return None
    try:
        return base.datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone(
            base.CAIRO
        ).strftime("%H:%M")
    except ValueError:
        return None


def _canonical_clock(record: dict[str, Any], spec: ScenarioSpec) -> str | None:
    if spec.clarification_required:
        return None
    for candidate in (
        _availability_clock(record),
        base._task_time(record),
        _proposal_clock(record),
        base._appointment_clock(record),
    ):
        if candidate:
            return candidate
    operation = _semantic_operation(record)
    time_value = _semantic_time(operation)
    raw = time_value.get("start_time")
    if operation.get("clock_source") == "literal_24h" and raw:
        return str(raw)
    return None


def _looks_like_period_question(reply: str) -> bool:
    lowered = reply.lower()
    morning = any(token in lowered for token in ("الصبح", "صباح", "صباحًا"))
    evening = any(token in lowered for token in ("بالليل", "مساء", "مساءً", "ليل"))
    return morning and evening


def _fallback_looking(reply: str) -> bool:
    lowered = reply.lower()
    return any(
        token in lowered
        for token in (
            "fallback",
            "system error",
            "internal error",
            "حصل خطأ",
            "تعذر تنفيذ",
        )
    )


def _validate_write_safety(record: dict[str, Any], spec: ScenarioSpec, ctx: base.Context) -> None:
    appointments = record.get("appointments") or []
    delta = int((record.get("db_effect") or {}).get("appointment_delta") or 0)
    _require(
        len(record.get("proposals_created") or []) <= 1,
        counter="duplicate_proposals",
        message=f"{spec.name}: duplicate proposals created",
    )
    _require(
        len(appointments) <= 1,
        counter="duplicate_appointments",
        message=f"{spec.name}: duplicate appointments created",
    )
    if spec.clarification_required or spec.unavailable:
        _require(
            delta == 0 and not appointments,
            counter="wrong_writes",
            message=f"{spec.name}: unresolved/unavailable clock wrote appointment",
        )
    if not appointments:
        return
    appointment = appointments[0]
    _require(
        base._appointment_clock(record) == spec.expected_canonical,
        counter="wrong_time_writes",
        message=f"{spec.name}: DB appointment used wrong clock",
    )
    _require(
        str(appointment.get("service_id") or "") == str(ctx.bikini.id),
        counter="lost_services",
        message=f"{spec.name}: DB appointment lost or changed requested service",
    )
    _require(
        str(appointment.get("doctor_id") or "") == str(ctx.doctor.id),
        counter="wrong_writes",
        message=f"{spec.name}: DB appointment used wrong doctor",
    )
    _require(
        appointment.get("laser_device_key") == "candela_gentle",
        counter="wrong_device",
        message=f"{spec.name}: DB appointment used wrong device",
    )
    _require(
        str(appointment.get("status") or "") == "pending",
        counter="false_success",
        message=f"{spec.name}: booking creation status is not pending",
    )
    device_keys = {
        str(value)
        for value in [
            appointment.get("laser_device_key"),
            *[
                line.get("laser_device_key")
                for line in appointment.get("additional_services") or []
                if isinstance(line, dict)
            ],
        ]
        if value not in (None, "")
    }
    _require(
        len(device_keys) <= 1,
        counter="cross_device_visits",
        message=f"{spec.name}: cross-device visit detected",
    )


def _validate_record(record: dict[str, Any], spec: ScenarioSpec, ctx: base.Context) -> None:
    operation = _semantic_operation(record)
    time_value = _semantic_time(operation)
    source = str(operation.get("clock_source") or "unspecified")
    period = str(operation.get("time_period_provenance") or "none")
    reply = str(record.get("agent") or "").strip()

    _require(
        source == spec.expected_source,
        counter="silent_am_pm_mutation",
        message=f"{spec.name}: typed clock_source={source}, expected {spec.expected_source}",
    )
    _require(
        period == spec.expected_period,
        counter="silent_am_pm_mutation",
        message=f"{spec.name}: period={period}, expected {spec.expected_period}",
    )
    if spec.expected_source == "bare_spoken_12h":
        _require(
            time_value.get("start_time_ambiguity") == "twelve_hour",
            counter="silent_am_pm_mutation",
            message=f"{spec.name}: bare clock was not emitted as twelve_hour semantic clock",
        )
        trace = _original_bare_trace()
        _require(
            trace is not None,
            counter="silent_am_pm_mutation",
            message=f"{spec.name}: missing deterministic bare-clock trace",
        )
        assert trace is not None
        _require(
            trace.get("status") == spec.expected_bare_status,
            counter=(
                "arbitrary_10_11_resolution"
                if spec.clarification_required
                else "silent_am_pm_mutation"
            ),
            message=(
                f"{spec.name}: bare resolution status={trace.get('status')}, "
                f"expected {spec.expected_bare_status}"
            ),
        )
        _require(
            tuple(trace.get("candidate_clocks") or []) == spec.expected_candidates,
            counter="silent_am_pm_mutation",
            message=f"{spec.name}: candidate clocks mismatch",
        )
        _require(
            tuple(trace.get("valid_operating_hour_candidates") or [])
            == spec.expected_valid_candidates,
            counter="silent_am_pm_mutation",
            message=f"{spec.name}: valid operating-hour candidates mismatch",
        )

    canonical = _canonical_clock(record, spec)
    _require(
        canonical == spec.expected_canonical,
        counter="silent_am_pm_mutation",
        message=(
            f"{spec.name}: canonical clock={canonical}, "
            f"expected {spec.expected_canonical}"
        ),
    )

    period_question = _looks_like_period_question(reply)
    if spec.clarification_required:
        _require(
            period_question,
            counter="arbitrary_10_11_resolution",
            message=f"{spec.name}: both candidates were valid but Agent did not clarify period",
        )
        _require(
            not record.get("availability_requests")
            and not (base._proposal(record) or record.get("booking_proposal")),
            counter="arbitrary_10_11_resolution",
            message=f"{spec.name}: unresolved 10/11 progressed to availability/proposal",
        )
    elif spec.name in {"RT2", "RT4", "RT5"}:
        _require(
            not period_question,
            counter="unnecessary_10_11_clarification",
            message=f"{spec.name}: clinic hours uniquely resolved 10/11 but Agent asked AM/PM",
        )

    if spec.unavailable:
        _require(
            not record.get("appointments"),
            counter="wrong_writes",
            message=f"{spec.name}: unavailable explicit time wrote appointment",
        )
        _require(
            _availability_clock(record) == spec.expected_canonical,
            counter="silent_am_pm_mutation",
            message=f"{spec.name}: unavailable explicit time mutated before availability",
        )

    _require(
        bool(reply) and not _fallback_looking(reply),
        counter="false_success",
        message=f"{spec.name}: empty/fallback-looking customer response",
    )
    _validate_write_safety(record, spec, ctx)

    trace = _original_bare_trace()
    candidates = (
        list(trace.get("candidate_clocks") or [])
        if trace is not None
        else ([spec.expected_canonical] if spec.expected_canonical else [])
    )
    valid_candidates = (
        list(trace.get("valid_operating_hour_candidates") or [])
        if trace is not None
        else ([spec.expected_canonical] if spec.expected_canonical else [])
    )
    record["clock_resolution_trace"] = list(CLOCK_TRACE)
    record["evidence"] = {
        "CUSTOMER": record.get("customer"),
        "AGENT": record.get("agent"),
        "Typed clock source": source,
        "Typed period provenance": period,
        "Typed semantic time": time_value,
        "Effective clinic/branch operating hours": {
            "requested_date": "2026-10-15",
            "weekday": 3,
            "branch_id": str(ctx.workspace.primary_branch_id),
            "start": spec.start,
            "end": spec.end,
        },
        "Candidate clocks": candidates,
        "Valid operating-hour candidates": valid_candidates,
        "Canonical resolved time": canonical,
        "Clarification required": spec.clarification_required,
        "Availability request": record.get("availability_requests") or [],
        "Proposal": base._proposal(record) or record.get("booking_proposal"),
        "DB effect": record.get("db_effect"),
    }
    record["assessment"] = {
        "Functional": "PASS",
        "Naturalness": "CLEAN",
        "Unnecessary clarification": "CLEAN",
        "Silent time mutation": "CLEAN",
        "False success": "CLEAN",
    }


def _task_constraint_facts(record: dict[str, Any]) -> dict[str, Any]:
    task = record.get("active_task")
    if not isinstance(task, dict):
        return {}
    constraints = task.get("constraints")
    return constraints if isinstance(constraints, dict) else {}


def _validate_rt3_continuation(
    first: dict[str, Any],
    continued: dict[str, Any],
    ctx: base.Context,
    *,
    expected_clock: str,
    expected_period: str,
) -> None:
    first_constraints = _task_constraint_facts(first)
    _require(
        bool(first_constraints),
        counter="lost_services",
        message="RT3: clarification did not preserve active booking state",
    )
    _require(
        str(first_constraints.get("service_id") or "") == str(ctx.bikini.id),
        counter="lost_services",
        message="RT3: clarification lost requested service",
    )
    _require(
        str(first_constraints.get("branch_id") or "") == str(ctx.workspace.primary_branch_id),
        counter="wrong_writes",
        message="RT3: clarification lost branch",
    )
    first_date = first_constraints.get("date")
    _require(
        isinstance(first_date, dict) and first_date.get("start_date") == "2026-10-15",
        counter="wrong_time_writes",
        message="RT3: clarification lost requested date",
    )

    continued_constraints = _task_constraint_facts(continued)
    if continued_constraints:
        continued_time = continued_constraints.get("time")
        _require(
            isinstance(continued_time, dict)
            and continued_time.get("start_time") == expected_clock
            and continued_time.get("start_time_ambiguity") == "none",
            counter="contextual_period_misresolution",
            message=(
                "RT3: contextual period resolution did not persist canonical time "
                f"{expected_clock}: {continued_time!r}"
            ),
        )
        _require(
            str(continued_constraints.get("time_clock_source") or "") == "explicit_period",
            counter="contextual_period_misresolution",
            message="RT3: resolved follow-up did not persist explicit period authority",
        )
        _require(
            str(continued_constraints.get("service_id") or "") == str(ctx.bikini.id),
            counter="lost_services",
            message="RT3: continuation changed requested service",
        )
        _require(
            str(continued_constraints.get("branch_id") or "") == str(ctx.workspace.primary_branch_id),
            counter="wrong_writes",
            message="RT3: continuation changed branch",
        )
        continued_date = continued_constraints.get("date")
        _require(
            isinstance(continued_date, dict)
            and continued_date.get("start_date") == "2026-10-15",
            counter="wrong_time_writes",
            message="RT3: continuation changed requested date",
        )

    canonical = next(
        (
            value
            for value in (
                _availability_clock(continued),
                base._task_time(continued),
                _proposal_clock(continued),
                base._appointment_clock(continued),
            )
            if value
        ),
        None,
    )
    _require(
        canonical == expected_clock,
        counter="contextual_period_misresolution",
        message=(
            f"RT3: {expected_period} continuation canonical clock={canonical}, "
            f"expected {expected_clock}"
        ),
    )
    availability = continued.get("availability_requests") or []
    if availability:
        _require(
            _availability_clock(continued) == expected_clock,
            counter="wrong_time_writes",
            message=f"RT3: continuation availability did not use {expected_clock}",
        )
    appointments = continued.get("appointments") or []
    _require(
        len(appointments) <= 1,
        counter="duplicate_appointments",
        message="RT3: continuation created duplicate appointments",
    )
    _require(
        len(continued.get("proposals_created") or []) <= 1,
        counter="duplicate_proposals",
        message="RT3: continuation created duplicate proposals",
    )
    if appointments:
        _require(
            base._appointment_clock(continued) == expected_clock,
            counter="wrong_time_writes",
            message=f"RT3: continuation wrote a non-{expected_clock} appointment",
        )
        _require(
            str(appointments[0].get("service_id") or "") == str(ctx.bikini.id),
            counter="lost_services",
            message="RT3: continuation changed requested service",
        )

    continued["rt3_continuation_evidence"] = {
        "CUSTOMER": continued.get("customer"),
        "AGENT": continued.get("agent"),
        "model": continued.get("model"),
        "raw_structured": continued.get("raw_structured") or [],
        "expected_period": expected_period,
        "canonical_time": canonical,
        "persisted_constraints": continued_constraints,
        "availability_request": availability,
        "proposal": base._proposal(continued) or continued.get("booking_proposal"),
        "DB_effect": continued.get("db_effect"),
        "same_service_id": str(first_constraints.get("service_id") or "") == str(ctx.bikini.id),
        "same_branch_id": str(first_constraints.get("branch_id") or "") == str(ctx.workspace.primary_branch_id),
        "same_date": isinstance(first_date, dict) and first_date.get("start_date") == "2026-10-15",
    }


def _run_scenario(ctx: base.Context, spec: ScenarioSpec) -> None:
    _set_operating_hours(ctx, start=spec.start, end=spec.end)
    CLOCK_TRACE.clear()
    record = base._chat(ctx, spec.customer)
    _validate_record(record, spec, ctx)
    if spec.name != "RT3":
        return

    mode = os.environ.get("CLOCK_RT3_CONTINUATION", "night").strip().lower()
    if mode == "night":
        reply = "بالليل"
        expected_clock = "22:00"
        expected_period = "pm"
    elif mode == "morning":
        reply = "الصبح"
        expected_clock = "10:00"
        expected_period = "am"
    else:
        raise ValueError(f"Unknown CLOCK_RT3_CONTINUATION: {mode}")

    CLOCK_TRACE.clear()
    continued = base._chat(ctx, reply)
    _validate_rt3_continuation(
        record,
        continued,
        ctx,
        expected_clock=expected_clock,
        expected_period=expected_period,
    )


def _persisted_appointments(workspace_id: UUID) -> int:
    return base._persisted_appointments(workspace_id)


def _selected_scenarios() -> list[ScenarioSpec]:
    raw = os.environ.get("CLOCK_RT_SCENARIOS", "").strip()
    if not raw:
        return list(SCENARIOS)
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    selected = [spec for spec in SCENARIOS if spec.name in requested]
    missing = requested - {spec.name for spec in selected}
    if missing:
        raise ValueError(f"Unknown CLOCK_RT_SCENARIOS: {sorted(missing)}")
    return selected


def main() -> int:
    results: list[dict[str, Any]] = []
    failures = 0
    scenarios = _selected_scenarios()
    for spec in scenarios:
        workspace_id: UUID | None = None
        ctx: base.Context | None = None
        turns: list[dict[str, Any]] = []
        error: str | None = None
        try:
            with base.rollback_session() as db:
                ctx = base._setup(db, spec.name, hole=False)
                workspace_id = ctx.workspace.id
                _run_scenario(ctx, spec)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            error = f"{type(exc).__name__}: {exc}"
        if ctx is not None:
            turns = list(ctx.turns)
        persisted = _persisted_appointments(workspace_id) if workspace_id is not None else 0
        if persisted:
            SAFETY_COUNTERS["persisted_audit_writes"] += persisted
            failures += 1
            error = (error + "; " if error else "") + f"persisted_audit_writes={persisted}"
        results.append(
            {
                "scenario": spec.name,
                "status": "PASS" if error is None else "FAIL",
                "error": error,
                "operating_hours": {"start": spec.start, "end": spec.end},
                "turns": turns,
                "persisted_appointments_after_rollback": persisted,
            }
        )
        print(
            "CLOCK_BOUNDARY_RESULT="
            + json.dumps(
                results[-1],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
            flush=True,
        )

    payload = {
        "status": "PASS" if failures == 0 else "FAIL",
        "scenario_count": len(results),
        "results": results,
        "safety_counters": SAFETY_COUNTERS,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        "CLOCK_BOUNDARY_SUMMARY="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        flush=True,
    )
    return 0 if failures == 0 and all(value == 0 for value in SAFETY_COUNTERS.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
