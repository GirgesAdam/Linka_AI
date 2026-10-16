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
from scripts import spoken_clock_live_review_base as base
from scripts.run_grouped_multiservice_intent_live_review import rollback_session

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
    "lost_period_hints": 0,
    "repeated_period_clarifications": 0,
    "stale_period_hint_retention": 0,
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
    expected_hint: str | None
    expected_canonical: str | None
    expected_bare_status: str | None = None
    expected_candidates: tuple[str, ...] = ()
    expected_valid_candidates: tuple[str, ...] = ()
    clarification_required: bool = False
    unavailable: bool = False


SCENARIOS = [
    ScenarioSpec(
        name="RA1",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 3\u061f",
        start="10:00", end="22:00", expected_hint=None, expected_canonical="15:00",
        expected_bare_status="resolved", expected_candidates=("03:00", "15:00"),
        expected_valid_candidates=("15:00",),
    ),
    ScenarioSpec(
        name="RA2",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 3 \u0627\u0644\u0635\u0628\u062d\u061f",
        start="10:00", end="22:00", expected_hint=None, expected_canonical="15:00",
        expected_bare_status="resolved", expected_candidates=("03:00", "15:00"),
        expected_valid_candidates=("15:00",),
    ),
    ScenarioSpec(
        name="RA3",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 3 \u0628\u0627\u0644\u0644\u064a\u0644\u061f",
        start="10:00", end="22:00", expected_hint=None, expected_canonical="15:00",
        expected_bare_status="resolved", expected_candidates=("03:00", "15:00"),
        expected_valid_candidates=("15:00",),
    ),
    ScenarioSpec(
        name="RA4",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 4 \u0648\u0631\u0628\u0639\u061f",
        start="10:00", end="22:00", expected_hint=None, expected_canonical="16:15",
        expected_bare_status="resolved", expected_candidates=("04:15", "16:15"),
        expected_valid_candidates=("16:15",),
    ),
    ScenarioSpec(
        name="RA5",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 10\u061f",
        start="00:00", end="23:59", expected_hint="none", expected_canonical=None,
        expected_bare_status="clarify_period", expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"), clarification_required=True,
    ),
    ScenarioSpec(
        name="RA6",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0628\u0627\u0644\u0644\u064a\u0644\u061f",
        start="00:00", end="23:59", expected_hint="pm", expected_canonical="22:00",
        expected_bare_status="resolved", expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"),
    ),
    ScenarioSpec(
        name="RA7",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0627\u0644\u0635\u0628\u062d\u061f",
        start="00:00", end="23:59", expected_hint="am", expected_canonical="10:00",
        expected_bare_status="resolved", expected_candidates=("10:00", "22:00"),
        expected_valid_candidates=("10:00", "22:00"),
    ),
    ScenarioSpec(
        name="RA8",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0648\u0646\u0635\u061f",
        start="00:00", end="23:59", expected_hint="none", expected_canonical=None,
        expected_bare_status="clarify_period", expected_candidates=("10:30", "22:30"),
        expected_valid_candidates=("10:30", "22:30"), clarification_required=True,
    ),
    ScenarioSpec(
        name="RA9",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0648\u0646\u0635 \u0628\u0627\u0644\u0644\u064a\u0644\u061f",
        start="00:00", end="23:59", expected_hint="pm", expected_canonical="22:30",
        expected_bare_status="resolved", expected_candidates=("10:30", "22:30"),
        expected_valid_candidates=("10:30", "22:30"),
    ),
    ScenarioSpec(
        name="RA10",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0648\u0646\u0635 \u0627\u0644\u0635\u0628\u062d\u061f",
        start="00:00", end="23:59", expected_hint="am", expected_canonical="10:30",
        expected_bare_status="resolved", expected_candidates=("10:30", "22:30"),
        expected_valid_candidates=("10:30", "22:30"),
    ),
    ScenarioSpec(
        name="RA11",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 11\u061f",
        start="00:00", end="23:59", expected_hint="none", expected_canonical=None,
        expected_bare_status="clarify_period", expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("11:00", "23:00"), clarification_required=True,
    ),
    ScenarioSpec(
        name="RA12",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 11 \u0628\u0627\u0644\u0644\u064a\u0644\u061f",
        start="00:00", end="23:59", expected_hint="pm", expected_canonical="23:00",
        expected_bare_status="resolved", expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("11:00", "23:00"),
    ),
    ScenarioSpec(
        name="RA13",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 11 \u0627\u0644\u0635\u0628\u062d\u061f",
        start="00:00", end="23:59", expected_hint="am", expected_canonical="11:00",
        expected_bare_status="resolved", expected_candidates=("11:00", "23:00"),
        expected_valid_candidates=("11:00", "23:00"),
    ),
    ScenarioSpec(
        name="RA14",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 03:00\u061f",
        start="10:00", end="22:00", expected_hint="none", expected_canonical="03:00",
        unavailable=True,
    ),
    ScenarioSpec(
        name="RA15",
        customer="\u0645\u0645\u0643\u0646 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u064a\u0648\u0645 \u0627\u0644\u062e\u0645\u064a\u0633 15 \u0623\u0643\u062a\u0648\u0628\u0631 2026 \u0627\u0644\u0633\u0627\u0639\u0629 12 \u0628\u0627\u0644\u0644\u064a\u0644\u061f",
        start="00:00", end="05:00", expected_hint="midnight", expected_canonical="00:00",
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
    if raw and time_value.get("start_time_ambiguity") != "twelve_hour":
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
        counter=None,
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


def _serialized_period_hint(time_payload: object) -> str:
    """Normalize runtime state serialization where the default `none` hint is omitted."""
    if not isinstance(time_payload, dict):
        return "none"
    return str(time_payload.get("spoken_period_hint") or "none")


def _raw_spoken_period_hint(record: dict[str, Any]) -> str:
    for payload in record.get("raw_structured") or []:
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
            if isinstance(time_value, dict):
                hint = str(time_value.get("spoken_period_hint") or "none")
                if hint != "none":
                    return hint
            selection = operation.get("selection")
            if isinstance(selection, dict):
                hint = str(selection.get("spoken_period_hint") or "none")
                if hint != "none":
                    return hint
    return "none"


def _transactional_safety_deltas(
    record: dict[str, Any],
    *,
    expected_clock: str | None,
) -> dict[str, int]:
    """Count unsafe transactional behavior before any rollback can hide it."""
    deltas = {"wrong_time_writes": 0, "false_success": 0}
    appointments = record.get("appointments") or []
    if not appointments or expected_clock is None:
        return deltas
    actual = base._appointment_clock(record)
    if actual is None or actual == expected_clock:
        return deltas
    deltas["wrong_time_writes"] = 1
    reply = str(record.get("agent") or "")
    if base._reply_mentions_clock(reply, actual):
        deltas["false_success"] = 1
    return deltas


def _record_transactional_safety(record: dict[str, Any], *, expected_clock: str | None) -> None:
    for key, value in _transactional_safety_deltas(record, expected_clock=expected_clock).items():
        SAFETY_COUNTERS[key] += value


def _validate_record(record: dict[str, Any], spec: ScenarioSpec, ctx: base.Context) -> None:
    operation = _semantic_operation(record)
    time_value = _semantic_time(operation)
    hint = _raw_spoken_period_hint(record)
    reply = str(record.get("agent") or "").strip()
    _record_transactional_safety(record, expected_clock=spec.expected_canonical)

    if spec.expected_hint is not None:
        _require(
            hint == spec.expected_hint,
            counter="silent_am_pm_mutation",
            message=f"{spec.name}: spoken_period_hint={hint}, expected {spec.expected_hint}",
        )
    if spec.expected_bare_status is not None:
        # Ambiguity is backend-derived authority, not an LLM/provider contract field.
        # Validate the deterministic resolver trace and canonical outcome instead.
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
    else:
        _require(
            not period_question,
            counter="unnecessary_10_11_clarification",
            message=f"{spec.name}: resolved clock triggered an unnecessary AM/PM clarification",
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
        "Backend bare-clock trace": _original_bare_trace(),
        "Raw spoken_period_hint": hint,
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


def _validate_ra_t10_continuation(
    first: dict[str, Any],
    continued: dict[str, Any],
    ctx: base.Context,
    *,
    expected_clock: str,
    expected_hint: str,
) -> None:
    first_constraints = _task_constraint_facts(first)
    _require(
        bool(first_constraints),
        counter="lost_services",
        message="RA-T10: clarification did not preserve active booking state",
    )
    _require(
        str(first_constraints.get("service_id") or "") == str(ctx.bikini.id),
        counter="lost_services",
        message="RA-T10: clarification lost requested service",
    )
    _require(
        str(first_constraints.get("branch_id") or "") == str(ctx.workspace.primary_branch_id),
        counter="wrong_writes",
        message="RA-T10: clarification lost branch",
    )
    first_date = first_constraints.get("date")
    _require(
        isinstance(first_date, dict) and first_date.get("start_date") == "2026-10-15",
        counter="wrong_time_writes",
        message="RA-T10: clarification lost requested date",
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
                "RA-T10: contextual period resolution did not persist canonical time "
                f"{expected_clock}: {continued_time!r}"
            ),
        )
        _require(
            str(continued_constraints.get("time_clock_source") or "") == "bare_spoken_12h",
            counter="contextual_period_misresolution",
            message="RA-T10: resolved follow-up did not preserve backend-owned spoken-clock authority",
        )
        _require(
            str(continued_constraints.get("service_id") or "") == str(ctx.bikini.id),
            counter="lost_services",
            message="RA-T10: continuation changed requested service",
        )
        _require(
            str(continued_constraints.get("branch_id") or "") == str(ctx.workspace.primary_branch_id),
            counter="wrong_writes",
            message="RA-T10: continuation changed branch",
        )
        continued_date = continued_constraints.get("date")
        _require(
            isinstance(continued_date, dict)
            and continued_date.get("start_date") == "2026-10-15",
            counter="wrong_time_writes",
            message="RA-T10: continuation changed requested date",
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
            f"RA-T10: {expected_hint} continuation canonical clock={canonical}, "
            f"expected {expected_clock}"
        ),
    )
    availability = continued.get("availability_requests") or []
    if availability:
        _require(
            _availability_clock(continued) == expected_clock,
            counter="wrong_time_writes",
            message=f"RA-T10: continuation availability did not use {expected_clock}",
        )
    appointments = continued.get("appointments") or []
    _require(
        len(appointments) <= 1,
        counter="duplicate_appointments",
        message="RA-T10: continuation created duplicate appointments",
    )
    _require(
        len(continued.get("proposals_created") or []) <= 1,
        counter="duplicate_proposals",
        message="RA-T10: continuation created duplicate proposals",
    )
    if appointments:
        _require(
            base._appointment_clock(continued) == expected_clock,
            counter="wrong_time_writes",
            message=f"RA-T10: continuation wrote a non-{expected_clock} appointment",
        )
        _require(
            str(appointments[0].get("service_id") or "") == str(ctx.bikini.id),
            counter="lost_services",
            message="RA-T10: continuation changed requested service",
        )

    continued["rt3_continuation_evidence"] = {
        "CUSTOMER": continued.get("customer"),
        "AGENT": continued.get("agent"),
        "model": continued.get("model"),
        "raw_structured": continued.get("raw_structured") or [],
        "expected_hint": expected_hint,
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


def _run_stateful_period_continuation(
    ctx: base.Context,
    *,
    scenario: str,
    first_customer: str,
    persisted_clock: str,
    expected_hint: str,
    expected_canonical: str,
) -> None:
    _set_operating_hours(ctx, start="00:00", end="23:59")
    CLOCK_TRACE.clear()
    first = base._chat(ctx, first_customer)
    first_constraints = _task_constraint_facts(first)
    first_time = first_constraints.get("time") if isinstance(first_constraints, dict) else None
    _require(
        isinstance(first_time, dict)
        and first_time.get("start_time") == persisted_clock
        and first_time.get("start_time_ambiguity") == "twelve_hour"
        and first_time.get("spoken_period_hint") == expected_hint,
        counter="lost_period_hints",
        message=f"{scenario}: unresolved explicit period hint was not persisted: {first_time!r}",
    )
    _require(
        not (first.get("availability_requests") or []) and not (first.get("appointments") or []),
        counter="wrong_time_writes",
        message=f"{scenario}: missing-date turn progressed to time-dependent availability/write",
    )
    _require(
        not _looks_like_period_question(str(first.get("agent") or "")),
        counter="repeated_period_clarifications",
        message=f"{scenario}: explicit period turn unnecessarily asked AM/PM before date",
    )

    CLOCK_TRACE.clear()
    continued = base._chat(ctx, "\u0627\u0644\u062e\u0645\u064a\u0633")
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
    _record_transactional_safety(continued, expected_clock=expected_canonical)
    _require(
        canonical == expected_canonical,
        counter="contextual_period_misresolution",
        message=(
            f"{scenario}: persisted {expected_hint} hint did not resolve to "
            f"{expected_canonical}: {canonical}"
        ),
    )
    _require(
        not _looks_like_period_question(str(continued.get("agent") or "")),
        counter="repeated_period_clarifications",
        message=f"{scenario}: period was asked again after explicit hint survived persistence",
    )
    continued_constraints = _task_constraint_facts(continued)
    continued_time = (
        continued_constraints.get("time") if isinstance(continued_constraints, dict) else None
    )
    if isinstance(continued_time, dict) and continued_time.get("start_time_ambiguity") == "none":
        _require(
            _serialized_period_hint(continued_time) == "none",
            counter="lost_period_hints",
            message=f"{scenario}: resolved canonical clock retained provider period authority",
        )
    continued["stateful_period_continuation_evidence"] = {
        "first_persisted_time": first_time,
        "second_canonical_time": canonical,
        "second_persisted_time": continued_time,
        "repeated_period_question": _looks_like_period_question(str(continued.get("agent") or "")),
    }


def _validate_stateful_same_clock_correction(
    *,
    corrected_time: object,
    corrected: dict[str, Any],
    final: dict[str, Any],
    scenario: str = "RA-S4",
) -> None:
    """Assert the explicit-bare correction without assuming default fields serialize."""
    _require(
        isinstance(corrected_time, dict)
        and corrected_time.get("start_time") == "10:30"
        and corrected_time.get("start_time_ambiguity") == "twelve_hour"
        and _serialized_period_hint(corrected_time) == "none",
        counter="stale_period_hint_retention",
        message=f"{scenario}: explicit same-clock correction retained stale PM: {corrected_time!r}",
    )
    _require(
        not (corrected.get("appointments") or []),
        counter="wrong_time_writes",
        message=f"{scenario}: correction wrote before missing context was resolved",
    )
    _require(
        not (corrected.get("availability_requests") or []),
        counter="arbitrary_10_11_resolution",
        message=f"{scenario}: bare correction started availability before date/period resolution",
    )
    _require(
        _looks_like_period_question(str(final.get("agent") or "")),
        counter="arbitrary_10_11_resolution",
        message=f"{scenario}: bare corrected 10:30 did not require AM/PM in both-valid context",
    )
    _require(
        not (final.get("appointments") or []),
        counter="wrong_time_writes",
        message=f"{scenario}: unresolved bare correction produced a write",
    )
    _require(
        not (final.get("availability_requests") or []),
        counter="arbitrary_10_11_resolution",
        message=f"{scenario}: unresolved bare correction progressed to availability",
    )


def _run_stateful_explicit_same_clock_correction(ctx: base.Context) -> None:
    scenario = "RA-S4"
    _set_operating_hours(ctx, start="00:00", end="23:59")
    first = base._chat(
        ctx,
        "\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0648\u0646\u0635 \u0628\u0627\u0644\u0644\u064a\u0644",
    )
    first_time = _task_constraint_facts(first).get("time")
    _require(
        isinstance(first_time, dict) and _serialized_period_hint(first_time) == "pm",
        counter="lost_period_hints",
        message=f"{scenario}: setup did not establish durable PM authority: {first_time!r}",
    )

    corrected = base._chat(
        ctx,
        "\u0644\u0627\u060c \u062e\u0644\u064a\u0647\u0627 10 \u0648\u0646\u0635 \u0628\u0633",
    )
    corrected_time = _task_constraint_facts(corrected).get("time")

    final = base._chat(ctx, "\u0627\u0644\u062e\u0645\u064a\u0633")
    _validate_stateful_same_clock_correction(
        corrected_time=corrected_time,
        corrected=corrected,
        final=final,
        scenario=scenario,
    )
    final["stateful_same_clock_correction_evidence"] = {
        "first_persisted_time": first_time,
        "corrected_persisted_time": corrected_time,
        "final_period_question": _looks_like_period_question(str(final.get("agent") or "")),
    }

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
    base.install_runtime_instrumentation()
    for spec in scenarios:
        workspace_id: UUID | None = None
        ctx: base.Context | None = None
        turns: list[dict[str, Any]] = []
        error: str | None = None
        try:
            with rollback_session() as db:
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

    stateful_specs = [
        (
            "RA-S1",
            lambda ctx: _run_stateful_period_continuation(
                ctx,
                scenario="RA-S1",
                first_customer="\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0648\u0646\u0635 \u0628\u0627\u0644\u0644\u064a\u0644",
                persisted_clock="10:30",
                expected_hint="pm",
                expected_canonical="22:30",
            ),
        ),
        (
            "RA-S2",
            lambda ctx: _run_stateful_period_continuation(
                ctx,
                scenario="RA-S2",
                first_customer="\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0627\u0644\u0633\u0627\u0639\u0629 10 \u0648\u0646\u0635 \u0627\u0644\u0635\u0628\u062d",
                persisted_clock="10:30",
                expected_hint="am",
                expected_canonical="10:30",
            ),
        ),
        (
            "RA-S3",
            lambda ctx: _run_stateful_period_continuation(
                ctx,
                scenario="RA-S3",
                first_customer="\u0639\u0627\u064a\u0632\u0629 \u0623\u062d\u062c\u0632 \u0644\u064a\u0632\u0631 \u0628\u0643\u064a\u0646\u064a \u0627\u0644\u0633\u0627\u0639\u0629 11 \u0648\u0646\u0635 \u0628\u0627\u0644\u0644\u064a\u0644",
                persisted_clock="11:30",
                expected_hint="pm",
                expected_canonical="23:30",
            ),
        ),
        ("RA-S4", _run_stateful_explicit_same_clock_correction),
    ]
    for scenario, runner in stateful_specs:
        workspace_id = None
        ctx = None
        turns = []
        error = None
        try:
            with rollback_session() as db:
                ctx = base._setup(db, scenario, hole=False)
                workspace_id = ctx.workspace.id
                runner(ctx)
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
        stateful_result = {
            "scenario": scenario,
            "status": "PASS" if error is None else "FAIL",
            "error": error,
            "operating_hours": {"start": "00:00", "end": "23:59"},
            "turns": turns,
            "persisted_appointments_after_rollback": persisted,
        }
        results.append(stateful_result)
        print(
            "CLOCK_BOUNDARY_RESULT="
            + json.dumps(
                stateful_result,
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
    base.uninstall_runtime_instrumentation()
    return 0 if failures == 0 and all(value == 0 for value in SAFETY_COUNTERS.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
