from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Literal

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import (
    DateConstraint,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnOperation,
)

BoundaryKind = Literal["lower", "upper", "exact"]
BareClockResolutionStatus = Literal[
    "not_applicable",
    "resolved",
    "needs_context",
    "clarify_period",
    "outside_operating_hours",
]


@dataclass(frozen=True)
class BareClockResolution:
    status: BareClockResolutionStatus
    constraint: TimeConstraint | None
    candidate_clocks: tuple[str, ...] = ()
    valid_operating_hour_candidates: tuple[str, ...] = ()
    operating_hours: tuple[tuple[int, str, str], ...] = ()
    branch_id: str | None = None
    requested_date: str | None = None


def _minutes(value: str) -> int:
    parsed = time.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


def _clock(minutes: int) -> str:
    normalized = minutes % (24 * 60)
    return f"{normalized // 60:02d}:{normalized % 60:02d}"


def _twelve_hour_candidates(value: str) -> tuple[int, int]:
    base = _minutes(value)
    alternate = (base + 12 * 60) % (24 * 60)
    return base, alternate


def _normalize_hours(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, object]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        weekday = row.get("weekday")
        start = str(row.get("start") or "")[:5]
        end = str(row.get("end") or "")[:5]
        if not isinstance(weekday, int) or not 0 <= weekday <= 6:
            continue
        if len(start) != 5 or len(end) != 5:
            continue
        try:
            if _minutes(end) <= _minutes(start):
                continue
        except ValueError:
            continue
        normalized.append({"weekday": weekday, "start": start, "end": end})
    normalized.sort(key=lambda row: (int(row["weekday"]), str(row["start"]), str(row["end"])))
    return normalized


def _operating_hours(context: SemanticContext) -> list[dict[str, object]]:
    return _normalize_hours(context.server_metadata.get("clinic_operating_hours"))


def _branch_resolution_catalog(context: SemanticContext) -> dict[str, object]:
    raw = context.server_metadata.get("branch_resolution_catalog")
    return dict(raw) if isinstance(raw, dict) else {}


def _catalog_branches(context: SemanticContext) -> list[dict[str, object]]:
    raw = _branch_resolution_catalog(context).get("branches")
    if not isinstance(raw, list):
        return []
    return [dict(row) for row in raw if isinstance(row, dict)]


def _branch_hours(context: SemanticContext, branch_id: str) -> list[dict[str, object]]:
    for branch in _catalog_branches(context):
        canonical = branch.get("id") or branch.get("branch_id")
        if canonical not in (None, "") and str(canonical) == branch_id:
            return _normalize_hours(branch.get("working_hours"))
    return []


def _default_branch_id(context: SemanticContext) -> str | None:
    catalog = _branch_resolution_catalog(context)
    primary = catalog.get("primary_branch_id")
    branches = _catalog_branches(context)
    known_ids = [
        str(branch.get("id") or branch.get("branch_id"))
        for branch in branches
        if (branch.get("id") or branch.get("branch_id")) not in (None, "")
    ]
    if primary not in (None, "") and str(primary) in known_ids:
        return str(primary)
    unique = list(dict.fromkeys(known_ids))
    if len(unique) == 1:
        return unique[0]
    return None


def _active_task_branch_id(context: SemanticContext) -> str | None:
    raw_task = context.model_input.get("active_task")
    if not isinstance(raw_task, dict):
        return None
    task_type = raw_task.get("task_type")
    if task_type == "booking":
        raw_constraints = raw_task.get("constraints")
    elif task_type == "reschedule":
        raw_constraints = raw_task.get("replacement")
    else:
        return None
    if not isinstance(raw_constraints, dict):
        return None
    branch_ref = raw_constraints.get("branch_ref")
    if branch_ref in (None, ""):
        return None
    return context.resolve(str(branch_ref), expected_kind="branch")


def _operation_branch_id(operation: TurnOperation, context: SemanticContext) -> str | None:
    branch = operation.entities.branch
    if branch is not None and branch.ref:
        resolved = context.resolve(branch.ref, expected_kind="branch")
        if resolved is not None:
            return resolved
    return _active_task_branch_id(context) or _default_branch_id(context)


def _relevant_weekdays(constraint: DateConstraint | None) -> set[int] | None:
    if constraint is None or constraint.mode in {"from_date", "next_available"}:
        return None
    if constraint.start_date is None:
        return None

    start = date.fromisoformat(constraint.start_date)
    if constraint.mode == "exact":
        return {start.weekday()}
    if constraint.mode != "range" or constraint.end_date is None:
        return None

    end = date.fromisoformat(constraint.end_date)
    span = (end - start).days
    if span >= 6:
        return set(range(7))
    return {(start + timedelta(days=offset)).weekday() for offset in range(span + 1)}


def _exact_weekday(constraint: DateConstraint | None) -> tuple[str, set[int]] | None:
    if constraint is None or constraint.mode != "exact" or constraint.start_date is None:
        return None
    requested = date.fromisoformat(constraint.start_date)
    return requested.isoformat(), {requested.weekday()}


def _candidate_is_open(
    candidate_minutes: int,
    *,
    rows: list[dict[str, object]],
    weekdays: set[int] | None,
    boundary: BoundaryKind,
) -> bool:
    for row in rows:
        weekday = row.get("weekday")
        if not isinstance(weekday, int):
            continue
        if weekdays is not None and weekday not in weekdays:
            continue
        start = str(row.get("start") or "")[:5]
        end = str(row.get("end") or "")[:5]
        if len(start) != 5 or len(end) != 5:
            continue
        try:
            start_minutes = _minutes(start)
            end_minutes = _minutes(end)
        except ValueError:
            continue
        if boundary == "upper":
            if start_minutes < candidate_minutes <= end_minutes:
                return True
        elif start_minutes <= candidate_minutes < end_minutes:
            return True
    return False


def _resolve_boundary(
    value: str | None,
    ambiguity: str,
    *,
    rows: list[dict[str, object]],
    weekdays: set[int] | None,
    boundary: BoundaryKind,
) -> tuple[str | None, str]:
    """Legacy twelve-hour resolver kept for untyped historical/internal callers."""
    if value is None:
        return None, "none"
    if ambiguity != "twelve_hour":
        return value, "none"
    if not rows:
        return None, "twelve_hour"

    valid = [
        candidate
        for candidate in dict.fromkeys(_twelve_hour_candidates(value))
        if _candidate_is_open(
            candidate,
            rows=rows,
            weekdays=weekdays,
            boundary=boundary,
        )
    ]
    if len(valid) != 1:
        return None, "twelve_hour"
    return _clock(valid[0]), "none"


def _active_task_date(context: SemanticContext) -> DateConstraint | None:
    raw_task = context.model_input.get("active_task")
    if not isinstance(raw_task, dict):
        return None
    task_type = raw_task.get("task_type")
    if task_type == "booking":
        raw_constraints = raw_task.get("constraints")
    elif task_type == "reschedule":
        raw_constraints = raw_task.get("replacement")
    else:
        return None
    if not isinstance(raw_constraints, dict):
        return None
    raw_date = raw_constraints.get("date")
    if not isinstance(raw_date, dict):
        return None
    try:
        return DateConstraint.model_validate(raw_date)
    except ValueError:
        return None


def _bare_clock_candidates(value: str) -> tuple[int, tuple[int, ...]]:
    raw = _minutes(value)
    hour = raw // 60
    minute = raw % 60
    hour_12 = hour % 12
    if hour_12 == 0:
        hour_12 = 12
    if hour_12 == 12:
        return hour_12, (12 * 60 + minute,)
    am = hour_12 * 60 + minute
    pm = (hour_12 + 12) * 60 + minute
    return hour_12, (am, pm)


def resolve_bare_clock_constraint(
    constraint: TimeConstraint | None,
    *,
    date_constraint: DateConstraint | None,
    context: SemanticContext,
    branch_id: str | None = None,
) -> BareClockResolution:
    if (
        constraint is None
        or constraint.mode != "exact"
        or constraint.start_time is None
    ):
        return BareClockResolution(status="not_applicable", constraint=constraint)
    if constraint.start_time_ambiguity != "twelve_hour":
        # A prior deterministic authority boundary (for example the existing #230
        # literal-HH:MM guard) may have already made this clock exact. Never reopen
        # a resolved clock merely because the model's source label was stale.
        return BareClockResolution(
            status="resolved",
            constraint=constraint,
            candidate_clocks=(constraint.start_time,),
            branch_id=branch_id,
            requested_date=(
                date_constraint.start_date
                if date_constraint is not None and date_constraint.mode == "exact"
                else None
            ),
        )

    hour_12, candidate_minutes = _bare_clock_candidates(constraint.start_time)
    candidate_clocks = tuple(_clock(candidate) for candidate in candidate_minutes)

    # Product semantic default: a bare 12 means noon. Clinic hours may make it
    # unavailable later, but must never reinterpret it as midnight.
    if hour_12 == 12:
        canonical = (
            f"00:{_minutes(constraint.start_time) % 60:02d}"
            if constraint.spoken_period_hint == "midnight"
            else candidate_clocks[0]
        )
        resolved = constraint.model_copy(
            update={
                "start_time": canonical,
                "start_time_ambiguity": "none",
                "spoken_period_hint": "none",
            }
        )
        return BareClockResolution(
            status="resolved",
            constraint=resolved,
            candidate_clocks=candidate_clocks,
            valid_operating_hour_candidates=candidate_clocks,
            branch_id=branch_id,
            requested_date=(
                date_constraint.start_date
                if date_constraint is not None and date_constraint.mode == "exact"
                else None
            ),
        )

    exact = _exact_weekday(date_constraint)
    if exact is None:
        return BareClockResolution(
            status="needs_context",
            constraint=constraint,
            candidate_clocks=candidate_clocks,
            branch_id=branch_id,
        )
    requested_date, weekdays = exact

    resolved_branch = branch_id or _default_branch_id(context)
    if resolved_branch is not None:
        rows = _branch_hours(context, resolved_branch)
    else:
        # Identical schedules across unresolved branches are safe semantic context;
        # semantic_context exposes them only when all branch schedules match.
        rows = _operating_hours(context)

    if not rows:
        return BareClockResolution(
            status="needs_context" if resolved_branch is None else "outside_operating_hours",
            constraint=constraint,
            candidate_clocks=candidate_clocks,
            branch_id=resolved_branch,
            requested_date=requested_date,
        )

    operating_hours = tuple(
        (int(row["weekday"]), str(row["start"]), str(row["end"]))
        for row in rows
        if int(row["weekday"]) in weekdays
    )
    valid_minutes = tuple(
        candidate
        for candidate in candidate_minutes
        if _candidate_is_open(
            candidate,
            rows=rows,
            weekdays=weekdays,
            boundary="exact",
        )
    )
    valid_clocks = tuple(_clock(candidate) for candidate in valid_minutes)

    # Explicit customer AM/PM remains semantic authority, while clinic hours
    # determine whether that literal meaning is feasible. The opposite period is
    # only a clarification candidate; it never becomes scheduling authority
    # without customer confirmation.
    if constraint.spoken_period_hint in {"am", "pm"}:
        chosen_index = 0 if constraint.spoken_period_hint == "am" else 1
        chosen = candidate_minutes[chosen_index]
        opposite = candidate_minutes[1 - chosen_index]
        if chosen in valid_minutes:
            resolved = constraint.model_copy(
                update={
                    "start_time": _clock(chosen),
                    "start_time_ambiguity": "none",
                    "spoken_period_hint": "none",
                }
            )
            return BareClockResolution(
                status="resolved",
                constraint=resolved,
                candidate_clocks=candidate_clocks,
                valid_operating_hour_candidates=valid_clocks,
                operating_hours=operating_hours,
                branch_id=resolved_branch,
                requested_date=requested_date,
            )
        if opposite in valid_minutes:
            return BareClockResolution(
                status="clarify_period",
                constraint=constraint,
                candidate_clocks=candidate_clocks,
                valid_operating_hour_candidates=valid_clocks,
                operating_hours=operating_hours,
                branch_id=resolved_branch,
                requested_date=requested_date,
            )
        return BareClockResolution(
            status="outside_operating_hours",
            constraint=constraint,
            candidate_clocks=candidate_clocks,
            valid_operating_hour_candidates=valid_clocks,
            operating_hours=operating_hours,
            branch_id=resolved_branch,
            requested_date=requested_date,
        )

    if hour_12 <= 9:
        pm_candidate = candidate_minutes[1]
        if pm_candidate in valid_minutes:
            chosen = pm_candidate
        elif candidate_minutes[0] in valid_minutes:
            chosen = candidate_minutes[0]
        else:
            return BareClockResolution(
                status="outside_operating_hours",
                constraint=constraint,
                candidate_clocks=candidate_clocks,
                valid_operating_hour_candidates=valid_clocks,
                operating_hours=operating_hours,
                branch_id=resolved_branch,
                requested_date=requested_date,
            )
        resolved = constraint.model_copy(
            update={
                "start_time": _clock(chosen),
                "start_time_ambiguity": "none",
                "spoken_period_hint": "none",
            }
        )
        return BareClockResolution(
            status="resolved",
            constraint=resolved,
            candidate_clocks=candidate_clocks,
            valid_operating_hour_candidates=valid_clocks,
            operating_hours=operating_hours,
            branch_id=resolved_branch,
            requested_date=requested_date,
        )

    # 10 and 11 are the only genuinely ambiguous bare product clocks.
    if len(valid_minutes) == 1:
        resolved = constraint.model_copy(
            update={
                "start_time": _clock(valid_minutes[0]),
                "start_time_ambiguity": "none",
                "spoken_period_hint": "none",
            }
        )
        return BareClockResolution(
            status="resolved",
            constraint=resolved,
            candidate_clocks=candidate_clocks,
            valid_operating_hour_candidates=valid_clocks,
            operating_hours=operating_hours,
            branch_id=resolved_branch,
            requested_date=requested_date,
        )
    if len(valid_minutes) == 2:
        return BareClockResolution(
            status="clarify_period",
            constraint=constraint,
            candidate_clocks=candidate_clocks,
            valid_operating_hour_candidates=valid_clocks,
            operating_hours=operating_hours,
            branch_id=resolved_branch,
            requested_date=requested_date,
        )
    return BareClockResolution(
        status="outside_operating_hours",
        constraint=constraint,
        candidate_clocks=candidate_clocks,
        valid_operating_hour_candidates=valid_clocks,
        operating_hours=operating_hours,
        branch_id=resolved_branch,
        requested_date=requested_date,
    )


def resolve_time_constraint_by_clinic_hours(
    constraint: TimeConstraint | None,
    *,
    date_constraint: DateConstraint | None,
    context: SemanticContext,
    branch_id: str | None = None,
) -> TimeConstraint | None:
    """Canonicalize unresolved bare clocks; exact semantic clocks are already authoritative."""
    if constraint is None:
        return None

    if constraint.mode == "exact":
        if constraint.start_time_ambiguity != "twelve_hour":
            return constraint
        resolution = resolve_bare_clock_constraint(
            constraint,
            date_constraint=date_constraint,
            context=context,
            branch_id=branch_id,
        )
        if resolution.status == "resolved":
            return resolution.constraint
        # Preserve unresolved semantic clock state. Planner decides whether missing
        # branch/date context should be gathered, whether 10/11 needs clarification,
        # or whether both candidates are outside operating hours.
        return constraint

    rows = _operating_hours(context)
    weekdays = _relevant_weekdays(date_constraint)

    start_boundary: BoundaryKind = "upper" if constraint.mode == "before" else "lower"
    if constraint.mode == "exact":
        start_boundary = "exact"
    resolved_start, start_ambiguity = _resolve_boundary(
        constraint.start_time,
        constraint.start_time_ambiguity,
        rows=rows,
        weekdays=weekdays,
        boundary=start_boundary,
    )
    if constraint.start_time is not None and resolved_start is None:
        return None

    resolved_end = constraint.end_time
    end_ambiguity = constraint.end_time_ambiguity
    if constraint.mode == "range":
        resolved_end, end_ambiguity = _resolve_boundary(
            constraint.end_time,
            constraint.end_time_ambiguity,
            rows=rows,
            weekdays=weekdays,
            boundary="upper",
        )
        if constraint.end_time is not None and resolved_end is None:
            return None

    return constraint.model_copy(
        update={
            "start_time": resolved_start,
            "end_time": resolved_end,
            "start_time_ambiguity": start_ambiguity,
            "end_time_ambiguity": end_ambiguity,
        }
    )


def classify_operation_bare_clock(
    operation: TurnOperation,
    context: SemanticContext,
    *,
    date_constraint: DateConstraint | None = None,
    branch_id: str | None = None,
) -> BareClockResolution:
    constraint = operation.entities.time
    if (
        constraint is None
        or constraint.mode != "exact"
        or constraint.start_time_ambiguity != "twelve_hour"
    ):
        return BareClockResolution(status="not_applicable", constraint=constraint)
    effective_date = date_constraint or operation.entities.date or _active_task_date(context)
    effective_branch = branch_id or _operation_branch_id(operation, context)
    return resolve_bare_clock_constraint(
        operation.entities.time,
        date_constraint=effective_date,
        context=context,
        branch_id=effective_branch,
    )


def _resolve_selection(
    selection: Selection | None,
    *,
    date_constraint: DateConstraint | None,
    context: SemanticContext,
    branch_id: str | None,
) -> Selection | None:
    if selection is None or selection.kind != "time" or selection.time is None:
        return selection
    if selection.time_ambiguity != "twelve_hour":
        return selection
    resolution = resolve_bare_clock_constraint(
        TimeConstraint(
            mode="exact",
            start_time=selection.time,
            start_time_ambiguity=selection.time_ambiguity,
            spoken_period_hint=selection.spoken_period_hint,
        ),
        date_constraint=date_constraint,
        context=context,
        branch_id=branch_id,
    )
    if resolution.status != "resolved" or resolution.constraint is None:
        return selection
    return selection.model_copy(
        update={
            "time": resolution.constraint.start_time,
            "time_ambiguity": resolution.constraint.start_time_ambiguity,
            "spoken_period_hint": resolution.constraint.spoken_period_hint,
        }
    )


def resolve_turn_times_by_clinic_hours(
    turn: TiaTurnUnderstanding,
    context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Normalize typed clock semantics before deterministic planning."""
    inherited_date = _active_task_date(context)
    operations = []
    for operation in turn.operations:
        date_constraint = operation.entities.date or inherited_date
        branch_id = _operation_branch_id(operation, context)
        resolved_time = resolve_time_constraint_by_clinic_hours(
            operation.entities.time,
            date_constraint=date_constraint,
            context=context,
            branch_id=branch_id,
        )
        entities = operation.entities.model_copy(update={"time": resolved_time})
        selection = _resolve_selection(
            operation.selection,
            date_constraint=date_constraint,
            context=context,
            branch_id=branch_id,
        )
        operations.append(
            operation.model_copy(update={"entities": entities, "selection": selection})
        )
    return turn.model_copy(update={"operations": operations})
