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
from scripts import spoken_clock_live_review_support as base

REPORT_PATH = Path(
    os.environ.get(
        "CLOCK_RT_REPORT_PATH",
        "artifacts/spoken-clock-policy-live-review.json",
    )
)

SAFETY_COUNTERS = {
    "wrong_time_writes": 0,
    "wrong_writes": 0,
    "partial_writes": 0,
    "false_success": 0,
    "unnecessary_am_pm": 0,
    "arbitrary_10_11_resolution": 0,
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



_CLOCK_TRACE_INSTALLED = False


def _install_clock_trace() -> None:
    global _CLOCK_TRACE_INSTALLED
    if _CLOCK_TRACE_INSTALLED:
        return
    time_resolution.resolve_bare_clock_constraint = _capture_bare_resolution
    _CLOCK_TRACE_INSTALLED = True


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    customer: str
    start: str
    end: str
    expected_canonical: str | None
    expected_hint: str = "none"
    expected_bare_status: str | None = None
    expected_candidates: tuple[str, ...] = ()
    expected_valid_candidates: tuple[str, ...] = ()
    clarification_required: bool = False
    unavailable: bool = False


SCENARIOS = [
    ScenarioSpec(
        name="RA-T1",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3",
        start="10:00",
        end="22:00",
        expected_canonical="15:00",
        expected_bare_status="resolved",
        expected_candidates=("03:00", "15:00"),
        expected_valid_candidates=("15:00",),
    ),
    ScenarioSpec(
        name="RA-T2",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 3 الصبح",
        start="10:00",
        end="22:00",
        expected_canonical="15:00",
        expected_hint="am",
        expected_bare_status="resolved",
        expected_candidates=("03:00", "15:00"),
        expected_valid_candidates=("15:00",),
    ),
    ScenarioSpec(
        name="RA-T3",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 4 وربع",
        start="10:00",
        end="22:00",
        expected_canonical="16:15",
        expected_bare_status="resolved",
        expected_candidates=("04:15", "16:15"),
        expected_valid_candidates=("16:15",),
    ),
    ScenarioSpec(
        name="RA-T4",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 10",
        start="09:00",
        end="23:30",
        expected_canonical=None,
        expected_bare_status="clarify_period",
        expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"),
        clarification_required=True,
    ),
    ScenarioSpec(
        name="RA-T5",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 10 بالليل",
        start="09:00",
        end="23:30",
        expected_canonical="22:00",
        expected_hint="pm",
        expected_bare_status="resolved",
        expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"),
    ),
    ScenarioSpec(
        name="RA-T6",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11",
        start="09:00",
        end="23:30",
        expected_canonical=None,
        expected_bare_status="clarify_period",
        expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("11:00", "23:00"),
        clarification_required=True,
    ),
    ScenarioSpec(
        name="RA-T7",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11 بالليل",
        start="09:00",
        end="23:30",
        expected_canonical="23:00",
        expected_hint="pm",
        expected_bare_status="resolved",
        expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("11:00", "23:00"),
    ),
    ScenarioSpec(
        name="RA-T8",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 11 بالليل",
        start="09:00",
        end="21:00",
        expected_canonical="11:00",
        expected_hint="pm",
        expected_bare_status="resolved",
        expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("11:00",),
    ),
    ScenarioSpec(
        name="RA-T9",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 03:00 بالضبط",
        start="10:00",
        end="22:00",
        expected_canonical="03:00",
        unavailable=True,
    ),
    ScenarioSpec(
        name="RA-T10",
        customer="عايزة أحجز ليزر بكيني يوم الخميس 15 أكتوبر 2026 الساعة 10",
        start="09:00",
        end="23:30",
        expected_canonical=None,
        expected_bare_status="clarify_period",
        expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"),
        clarification_required=True,
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
            selection = operation.get("selection")
            if isinstance(time_value, dict) and time_value.get("start_time") not in (None, ""):
                return operation
            if isinstance(selection, dict) and selection.get("kind") == "time":
                return operation
    return {}


def _semantic_time(operation: dict[str, Any]) -> dict[str, Any]:
    entities = operation.get("entities")
    value = entities.get("time") if isinstance(entities, dict) else None
    if isinstance(value, dict):
        return value
    selection = operation.get("selection")
    return selection if isinstance(selection, dict) else {}


def _original_bare_trace() -> dict[str, Any] | None:
    for item in CLOCK_TRACE:
        raw = item.get("input_time")
        if isinstance(raw, dict) and raw.get("start_time_ambiguity") == "twelve_hour":
            return item
    return None


def _availability_clock(record: dict[str, Any]) -> str | None:
    for request in record.get("availability_requests") or []:
        if not isinstance(request, dict):
            continue
        value = request.get("time")
        if isinstance(value, dict) and value.get("start_time"):
            return str(value["start_time"])
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
    value = _semantic_time(operation)
    raw = value.get("start_time") or value.get("time")
    if raw and value.get("start_time_ambiguity", value.get("time_ambiguity")) != "twelve_hour":
        return str(raw)
    return None


def _looks_like_period_question(reply: str) -> bool:
    lowered = reply.lower()
    morning = any(token in lowered for token in ("الصبح", "صباح", "morning", "am"))
    evening = any(token in lowered for token in ("بالليل", "مساء", "ليل", "night", "pm"))
    return morning and evening


def _fallback_looking(reply: str) -> bool:
    lowered = reply.lower()
    return any(
        token in lowered
        for token in ("fallback", "system error", "internal error", "حصل خطأ", "تعذر تنفيذ")
    )


def _observe_transactional_safety(
    record: dict[str, Any],
    spec: ScenarioSpec,
    ctx: base.Context,
) -> None:
    """Count transactional defects immediately, independently from rollback persistence."""
    appointments = record.get("appointments") or []
    delta = int((record.get("db_effect") or {}).get("appointment_delta") or 0)
    if len(appointments) > 1:
        SAFETY_COUNTERS["duplicate_appointments"] += 1
    if len(record.get("proposals_created") or []) > 1:
        SAFETY_COUNTERS["duplicate_proposals"] += 1
    if (spec.clarification_required or spec.unavailable) and (delta != 0 or appointments):
        SAFETY_COUNTERS["wrong_writes"] += 1
    if len(appointments) != 1:
        return
    actual = base._appointment_clock(record)
    wrong_time = spec.expected_canonical is None or actual != spec.expected_canonical
    if wrong_time:
        SAFETY_COUNTERS["wrong_time_writes"] += 1
        if str(record.get("agent") or "").strip() and not _fallback_looking(
            str(record.get("agent") or "")
        ):
            SAFETY_COUNTERS["false_success"] += 1
    appointment = appointments[0]
    if str(appointment.get("service_id") or "") != str(ctx.bikini.id):
        SAFETY_COUNTERS["lost_services"] += 1
    if appointment.get("laser_device_key") != "candela_gentle":
        SAFETY_COUNTERS["wrong_device"] += 1
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
    if len(device_keys) > 1:
        SAFETY_COUNTERS["cross_device_visits"] += 1


def _validate_record(record: dict[str, Any], spec: ScenarioSpec, ctx: base.Context) -> None:
    operation = _semantic_operation(record)
    semantic = _semantic_time(operation)
    raw_hint = str(semantic.get("spoken_period_hint") or "none")
    reply = str(record.get("agent") or "").strip()

    _require(
        raw_hint == spec.expected_hint,
        message=f"{spec.name}: spoken_period_hint={raw_hint}, expected {spec.expected_hint}",
    )
    if spec.expected_bare_status is not None:
        trace = _original_bare_trace()
        _require(trace is not None, message=f"{spec.name}: missing deterministic clock trace")
        assert trace is not None
        _require(
            trace.get("status") == spec.expected_bare_status,
            counter=("arbitrary_10_11_resolution" if spec.clarification_required else None),
            message=f"{spec.name}: resolution status={trace.get('status')}",
        )
        _require(
            tuple(trace.get("candidate_clocks") or []) == spec.expected_candidates,
            message=f"{spec.name}: candidate clocks mismatch",
        )
        _require(
            tuple(trace.get("valid_operating_hour_candidates") or []) == spec.expected_valid_candidates,
            message=f"{spec.name}: valid clinic-hour candidates mismatch",
        )

    canonical = _canonical_clock(record, spec)
    _require(
        canonical == spec.expected_canonical,
        message=f"{spec.name}: canonical clock={canonical}, expected {spec.expected_canonical}",
    )

    asks_period = _looks_like_period_question(reply)
    if spec.clarification_required:
        _require(
            asks_period,
            counter="arbitrary_10_11_resolution",
            message=f"{spec.name}: both candidates valid but no period clarification",
        )
        _require(
            not record.get("appointments") and not (base._proposal(record) or record.get("booking_proposal")),
            counter="arbitrary_10_11_resolution",
            message=f"{spec.name}: unresolved 10/11 progressed to proposal/write",
        )
    elif spec.name in {"RA-T1", "RA-T2", "RA-T3", "RA-T5", "RA-T7", "RA-T8"}:
        _require(
            not asks_period,
            counter="unnecessary_am_pm",
            message=f"{spec.name}: unnecessary AM/PM clarification",
        )

    _require(bool(reply) and not _fallback_looking(reply), message=f"{spec.name}: empty/fallback reply")

    trace = _original_bare_trace()
    record["clock_resolution_trace"] = list(CLOCK_TRACE)
    record["evidence"] = {
        "customer": record.get("customer"),
        "agent": record.get("agent"),
        "raw_model_start_time": semantic.get("start_time") or semantic.get("time"),
        "raw_spoken_period_hint": raw_hint,
        "normalized_12h_base": (trace or {}).get("input_time"),
        "candidate_clocks": list((trace or {}).get("candidate_clocks") or []),
        "clinic_hours": list((trace or {}).get("operating_hours") or []),
        "valid_candidates": list((trace or {}).get("valid_operating_hour_candidates") or []),
        "chosen_canonical_clock": canonical,
        "clarification": asks_period,
        "proposal_time": _proposal_clock(record),
        "db_effect": record.get("db_effect"),
    }


def _task_constraints(record: dict[str, Any]) -> dict[str, Any]:
    task = record.get("active_task")
    if not isinstance(task, dict):
        return {}
    constraints = task.get("constraints")
    return constraints if isinstance(constraints, dict) else {}


def _validate_t10_side_question(first: dict[str, Any], side: dict[str, Any], final: dict[str, Any], ctx: base.Context) -> None:
    first_constraints = _task_constraints(first)
    side_constraints = _task_constraints(side)
    final_constraints = _task_constraints(final)
    _require(bool(first_constraints), counter="lost_services", message="RA-T10 lost active task on clarification")
    _require(
        str(first_constraints.get("service_id") or "") == str(ctx.bikini.id),
        counter="lost_services",
        message="RA-T10 lost service before continuation",
    )
    _require(
        side_constraints.get("service_id") == first_constraints.get("service_id")
        and side_constraints.get("date") == first_constraints.get("date")
        and side_constraints.get("time") == first_constraints.get("time"),
        counter="lost_services",
        message="RA-T10 side question changed pending task constraints",
    )
    final_clock = next(
        (
            value
            for value in (
                _availability_clock(final),
                base._task_time(final),
                _proposal_clock(final),
                base._appointment_clock(final),
            )
            if value
        ),
        None,
    )
    _require(final_clock == "22:00", counter="wrong_time_writes", message=f"RA-T10 final clock={final_clock}")
    if final_constraints:
        _require(
            str(final_constraints.get("service_id") or "") == str(ctx.bikini.id),
            counter="lost_services",
            message="RA-T10 continuation changed service",
        )
    _require(
        len(final.get("appointments") or []) <= 1,
        counter="duplicate_appointments",
        message="RA-T10 duplicate appointment",
    )
    _require(
        len(final.get("proposals_created") or []) <= 1,
        counter="duplicate_proposals",
        message="RA-T10 duplicate proposal",
    )
    final["evidence"] = {
        "customer": final.get("customer"),
        "agent": final.get("agent"),
        "raw_structured": final.get("raw_structured"),
        "raw_spoken_period_hint": base._raw_spoken_period_hint(final),
        "canonical_clock": final_clock,
        "active_task_before": final.get("active_task_before"),
        "active_task_after": final.get("active_task"),
        "db_effect": final.get("db_effect"),
    }


def _run_scenario(ctx: base.Context, spec: ScenarioSpec) -> None:
    _set_operating_hours(ctx, start=spec.start, end=spec.end)
    CLOCK_TRACE.clear()
    record = base._chat(ctx, spec.customer)
    _observe_transactional_safety(record, spec, ctx)
    _validate_record(record, spec, ctx)
    if spec.name != "RA-T10":
        return

    CLOCK_TRACE.clear()
    side = base._chat(ctx, "طب الجلسة مدتها قد إيه؟")
    _require(
        bool(str(side.get("agent") or "").strip()),
        message="RA-T10 side question returned empty reply",
    )
    CLOCK_TRACE.clear()
    final = base._chat(ctx, "بالليل")
    continuation_spec = ScenarioSpec(
        name="RA-T10-continuation",
        customer="بالليل",
        start=spec.start,
        end=spec.end,
        expected_canonical="22:00",
        expected_hint="pm",
        expected_bare_status="resolved",
        expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"),
    )
    _observe_transactional_safety(final, continuation_spec, ctx)
    _validate_t10_side_question(record, side, final, ctx)


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
    base.install_instrumentation()
    _install_clock_trace()
    results: list[dict[str, Any]] = []
    failures = 0
    for spec in _selected_scenarios():
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
        persisted = base._persisted_appointments(workspace_id) if workspace_id is not None else 0
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
            "SPOKEN_CLOCK_RESULT="
            + json.dumps(results[-1], ensure_ascii=False, separators=(",", ":"), default=str),
            flush=True,
        )

    payload = {
        "status": "PASS" if failures == 0 and all(v == 0 for v in SAFETY_COUNTERS.values()) else "FAIL",
        "scenario_count": len(results),
        "results": results,
        "safety_counters": SAFETY_COUNTERS,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        "SPOKEN_CLOCK_SUMMARY="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        flush=True,
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
