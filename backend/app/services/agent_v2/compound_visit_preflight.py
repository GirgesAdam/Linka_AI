from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.agents.clinic_grounding import build_clinic_catalog
from app.integrations.clinic.base import AvailabilityRequest, AvailabilityResult, AvailabilitySlot
from app.integrations.clinic.registry import get_clinic_adapter
from app.models.service import Service
from app.services.agent_v2.branch_resolution import resolve_single_location_branch_id
from app.services.agent_v2.compound_turn_policy import (
    compound_anchor_key,
    compound_sequence_index,
    compound_visit_group,
)
from app.services.agent_v2.planner import PlanStep, TurnPlan
from app.services.agent_v2.read_executor import ReadExecutionContext

_SEARCH_DAYS = 14
_SEQUENCE_FACT_KEYS = frozenset(
    {
        "compound_visit_sequenced",
        "compound_visit_sequence_index",
        "compound_visit_anchor_local",
        "compound_visit_selected_start_local",
    }
)


def _write_kind(step: PlanStep) -> str | None:
    return step.write_intent.kind if step.write_intent is not None else None


def _params(step: PlanStep) -> dict[str, object]:
    if step.write_intent is not None:
        return dict(step.write_intent.parameters)
    for request in step.reads:
        if request.kind == "availability":
            return dict(request.parameters)
    return dict(step.facts)


def _strip_sequence_facts(facts: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in facts.items() if key not in _SEQUENCE_FACT_KEYS}


def _service_buffers(context: ReadExecutionContext, service_id: object) -> tuple[int, int]:
    """Read native Linka service buffers when available; external adapters safely fall back to zero."""
    try:
        service_uuid = UUID(str(service_id))
        row = context.db.get(Service, service_uuid)
    except Exception:
        return 0, 0
    if row is None or getattr(row, "workspace_id", None) != context.workspace.id:
        return 0, 0
    return int(row.buffer_before_minutes or 0), int(row.buffer_after_minutes or 0)


def _slot_interval_minutes(results: list[AvailabilityResult]) -> int:
    differences: list[int] = []
    for result in results:
        starts = sorted({slot.start_at for slot in result.slots})
        for left, right in zip(starts, starts[1:], strict=False):
            minutes = int((right - left).total_seconds() // 60)
            if minutes > 0:
                differences.append(minutes)
    return min(differences) if differences else 15


def _ceil_local(value: datetime, interval_minutes: int, timezone_name: str) -> datetime:
    local = value.astimezone(ZoneInfo(timezone_name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((local - midnight).total_seconds() // 60)
    remainder = elapsed % interval_minutes
    if remainder == 0 and local.second == 0 and local.microsecond == 0:
        return local.astimezone(UTC)
    rounded = local + timedelta(minutes=interval_minutes - remainder)
    return rounded.replace(second=0, microsecond=0).astimezone(UTC)


def _same_resource(left: AvailabilitySlot, right: AvailabilitySlot) -> bool:
    if left.doctor_id == right.doctor_id:
        return True
    return bool(
        left.laser_device_key
        and right.laser_device_key
        and left.laser_device_key == right.laser_device_key
    )


def _required_next_start(
    selected: list[AvailabilitySlot],
    *,
    next_service_id: object,
    context: ReadExecutionContext,
    interval_minutes: int,
    timezone_name: str,
    candidate_resource: AvailabilitySlot,
) -> datetime:
    previous = selected[-1]
    required = previous.end_at
    next_before, _next_after = _service_buffers(context, next_service_id)
    next_before_delta = timedelta(minutes=next_before)
    for prior in selected:
        if not _same_resource(prior, candidate_resource):
            continue
        _prior_before, prior_after = _service_buffers(context, prior.service_id)
        resource_required = prior.end_at + timedelta(minutes=prior_after) + next_before_delta
        if resource_required > required:
            required = resource_required
    return _ceil_local(required, interval_minutes, timezone_name)


def _chain_for_first(
    first: AvailabilitySlot,
    results: list[AvailabilityResult],
    steps: list[PlanStep],
    *,
    context: ReadExecutionContext,
    timezone_name: str,
) -> list[AvailabilitySlot] | None:
    selected = [first]
    interval = _slot_interval_minutes(results)
    for result, step in zip(results[1:], steps[1:], strict=True):
        params = _params(step)
        matches: list[AvailabilitySlot] = []
        for slot in result.slots:
            required = _required_next_start(
                selected,
                next_service_id=params.get("service_id"),
                context=context,
                interval_minutes=interval,
                timezone_name=timezone_name,
                candidate_resource=slot,
            )
            if slot.start_at == required:
                matches.append(slot)
        if not matches:
            return None
        matches.sort(key=lambda slot: (slot.start_at, slot.doctor_id, slot.laser_device_key or ""))
        selected.append(matches[0])
    return selected


def _availability_for_day(
    steps: list[PlanStep],
    *,
    booking_date: date,
    context: ReadExecutionContext,
    forced_doctor_id: str | None = None,
) -> list[AvailabilityResult] | None:
    adapter = context.adapter or get_clinic_adapter(db=context.db, workspace=context.workspace)
    branch_default: str | None = None
    branch_default_resolved = False
    results: list[AvailabilityResult] = []
    for step in steps:
        params = _params(step)
        service_id = params.get("service_id")
        doctor_id = forced_doctor_id or params.get("doctor_id")
        branch_id = params.get("branch_id")
        if branch_id is None:
            if not branch_default_resolved:
                catalog = context.catalog
                if catalog is None and context.workspace.primary_branch_id is None:
                    catalog = build_clinic_catalog(context.db, context.workspace)
                branch_default = resolve_single_location_branch_id(
                    primary_branch_id=context.workspace.primary_branch_id,
                    catalog=catalog or {},
                )
                branch_default_resolved = True
            branch_id = branch_default
        if service_id is None or doctor_id is None or branch_id is None:
            return None
        if bool(step.facts.get("service_requires_laser_device")) and not params.get("device_key"):
            return None
        result = adapter.get_availability(
            AvailabilityRequest(
                branch_id=str(branch_id),
                service_id=str(service_id),
                booking_date=booking_date,
                doctor_id=str(doctor_id),
                now=context.now,
                laser_device_key=str(params["device_key"]) if params.get("device_key") else None,
            )
        )
        results.append(result)
    return results


def _find_joint_chain(
    steps: list[PlanStep],
    *,
    requested_anchor: datetime,
    context: ReadExecutionContext,
    timezone_name: str,
    forced_doctor_id: str | None = None,
) -> tuple[list[AvailabilitySlot] | None, bool]:
    tz = ZoneInfo(timezone_name)
    anchor_aware = requested_anchor.replace(tzinfo=tz).astimezone(UTC)
    for offset in range(_SEARCH_DAYS):
        current_date = requested_anchor.date() + timedelta(days=offset)
        results = _availability_for_day(
            steps,
            booking_date=current_date,
            context=context,
            forced_doctor_id=forced_doctor_id,
        )
        if results is None:
            return None, False
        first_slots = sorted(
            (
                slot
                for slot in results[0].slots
                if offset > 0 or slot.start_at >= anchor_aware
            ),
            key=lambda slot: (slot.start_at, slot.doctor_id, slot.laser_device_key or ""),
        )
        for first in first_slots:
            chain = _chain_for_first(
                first,
                results,
                steps,
                context=context,
                timezone_name=timezone_name,
            )
            if chain is not None:
                return chain, first.start_at == anchor_aware
    return None, False


def _constraints_for_slot(slot: AvailabilitySlot, timezone_name: str) -> tuple[dict[str, object], dict[str, object]]:
    local = slot.start_at.astimezone(ZoneInfo(timezone_name))
    return (
        {"mode": "exact", "start_date": local.date().isoformat(), "end_date": None},
        {
            "mode": "exact",
            "start_time": local.strftime("%H:%M"),
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        },
    )


def _rewrite_step_for_slot(
    step: PlanStep,
    slot: AvailabilitySlot,
    *,
    timezone_name: str,
    requested_anchor: datetime,
    allow_write: bool,
) -> PlanStep:
    date_constraint, time_constraint = _constraints_for_slot(slot, timezone_name)
    base_params = _params(step)
    parameters: dict[str, object] = {
        **base_params,
        "branch_id": slot.branch_id,
        "service_id": slot.service_id,
        "doctor_id": slot.doctor_id,
        "date": date_constraint,
        "time": time_constraint,
    }
    if slot.laser_device_key:
        parameters["device_key"] = slot.laser_device_key
    reads = [
        request.model_copy(
            update={
                "parameters": {
                    **request.parameters,
                    **parameters,
                }
            }
        )
        if request.kind == "availability"
        else request
        for request in step.reads
    ]
    facts = {
        **_strip_sequence_facts(step.facts),
        "date": date_constraint,
        "time": time_constraint,
        "exact_time_requested": allow_write,
        "compound_visit_preflight_resolved": True,
        "compound_visit_requested_anchor_local": requested_anchor.isoformat(timespec="minutes"),
        "compound_visit_alternative": not allow_write,
    }
    write_intent = None
    if allow_write and step.write_intent is not None:
        write_intent = step.write_intent.model_copy(update={"parameters": parameters})
    return step.model_copy(
        update={
            "disposition": "read",
            "reads": reads,
            "write_intent": write_intent,
            "state_action": step.state_action if allow_write else "none",
            "response_goal": step.response_goal if allow_write else "present_availability",
            "facts": facts,
        }
    )


def _missing_required_device(step: PlanStep) -> bool:
    return (
        bool(step.facts.get("service_requires_laser_device"))
        and not _params(step).get("device_key")
    )


def _clarify_missing_group_device(step: PlanStep, *, is_missing: bool) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "clarify" if is_missing else "blocked",
            "reads": step.reads if is_missing else [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "clarification",
            "clarification_field": "device" if is_missing else None,
            "facts": {
                **_strip_sequence_facts(step.facts),
                "compound_visit_preflight_resolved": True,
                "compound_visit_missing_device": True,
            },
        }
    )


def _suppress_group_write(step: PlanStep, *, requested_anchor: datetime) -> PlanStep:
    return step.model_copy(
        update={
            "disposition": "blocked",
            "reads": [],
            "write_intent": None,
            "state_action": "none",
            "response_goal": "no_availability",
            "facts": {
                **_strip_sequence_facts(step.facts),
                "compound_visit_preflight_resolved": True,
                "compound_visit_no_joint_window": True,
                "compound_visit_requested_anchor_local": requested_anchor.isoformat(timespec="minutes"),
                "compound_visit_search_days": _SEARCH_DAYS,
            },
        }
    )



def _doctor_candidates(step: PlanStep, context: ReadExecutionContext) -> set[str]:
    params = _params(step)
    doctor_id = params.get("doctor_id")
    if doctor_id:
        return {str(doctor_id)}
    raw_ids = params.get("doctor_ids")
    if isinstance(raw_ids, list):
        ids = {str(item) for item in raw_ids if item}
        if ids:
            return ids

    service_id = params.get("service_id")
    catalog = context.catalog if isinstance(context.catalog, dict) else {}
    doctors = catalog.get("doctors") if isinstance(catalog, dict) else None
    if service_id is None or not isinstance(doctors, list):
        return set()
    return {
        str(row["id"])
        for row in doctors
        if isinstance(row, dict)
        and row.get("id")
        and isinstance(row.get("service_ids"), list)
        and str(service_id) in {str(value) for value in row["service_ids"]}
    }


def _common_doctors(steps: list[PlanStep], context: ReadExecutionContext) -> list[str]:
    candidate_sets = [_doctor_candidates(step, context) for step in steps]
    if not candidate_sets or any(not values for values in candidate_sets):
        return []
    common = set.intersection(*candidate_sets)
    return sorted(common)


def _requested_group_anchor(
    steps: list[PlanStep],
    *,
    context: ReadExecutionContext,
    timezone_name: str,
) -> tuple[datetime, str] | None:
    compound_anchors = {
        value
        for step in steps
        if (value := compound_anchor_key(step)) is not None
    }
    if len(compound_anchors) == 1:
        try:
            return datetime.fromisoformat(next(iter(compound_anchors))), "exact"
        except ValueError:
            return None

    dates: list[dict[str, object]] = []
    times: list[dict[str, object] | None] = []
    for step in steps:
        params = _params(step)
        raw_date = params.get("date")
        raw_time = params.get("time")
        if not isinstance(raw_date, dict):
            return None
        dates.append(raw_date)
        times.append(raw_time if isinstance(raw_time, dict) else None)

    date_modes = {str(item.get("mode")) for item in dates}
    tz = ZoneInfo(timezone_name)
    if date_modes == {"next_available"}:
        local_now = context.now.astimezone(tz)
        return local_now.replace(tzinfo=None), "next_available"
    if date_modes != {"exact"}:
        return None

    exact_dates = {str(item.get("start_date")) for item in dates if item.get("start_date")}
    if len(exact_dates) != 1:
        return None
    day = date.fromisoformat(next(iter(exact_dates)))
    exact_times = {
        str(item.get("start_time"))
        for item in times
        if isinstance(item, dict) and item.get("mode") == "exact" and item.get("start_time")
    }
    if len(exact_times) == 1 and all(
        isinstance(item, dict) and item.get("mode") == "exact" for item in times
    ):
        return datetime.combine(day, datetime.strptime(next(iter(exact_times)), "%H:%M").time()), "exact"
    return datetime.combine(day, datetime.min.time()), "date_only"


def _attach_visit_group(step: PlanStep, visit_group_id: str, doctor_id: str) -> PlanStep:
    reads = []
    for request in step.reads:
        parameters = dict(request.parameters)
        if request.kind == "availability":
            parameters.pop("doctor_ids", None)
            parameters["doctor_id"] = doctor_id
        reads.append(request.model_copy(update={"parameters": parameters}))

    write_intent = step.write_intent
    if write_intent is not None:
        parameters = dict(write_intent.parameters)
        parameters.pop("doctor_ids", None)
        parameters["doctor_id"] = doctor_id
        parameters["visit_group_id"] = visit_group_id
        write_intent = write_intent.model_copy(update={"parameters": parameters})

    facts = dict(step.facts)
    facts.pop("doctor_ids", None)
    facts.update(
        {
            "doctor_id": doctor_id,
            "visit_group_id": visit_group_id,
            "compound_visit_auto_doctor": True,
            "compound_visit_preflight_resolved": True,
        }
    )
    return step.model_copy(update={"reads": reads, "write_intent": write_intent, "facts": facts})


def _auto_resolve_grouped_visits(
    plan: TurnPlan,
    *,
    context: ReadExecutionContext,
    timezone_name: str,
    visit_group_id: str | None,
) -> TurnPlan:
    groups: dict[str, list[PlanStep]] = {}
    for step in plan.steps:
        group = compound_visit_group(step)
        has_availability_read = any(request.kind == "availability" for request in step.reads)
        if group is not None and (_write_kind(step) == "booking" or has_availability_read):
            groups.setdefault(group, []).append(step)
    if not groups:
        return plan

    replacements: dict[int, PlanStep] = {}
    for _group_key, raw_group in groups.items():
        ordered = sorted(raw_group, key=lambda step: step.operation_index)
        if len(ordered) < 2:
            continue
        missing_device_indexes = {
            step.operation_index for step in ordered if _missing_required_device(step)
        }
        if missing_device_indexes:
            for step in ordered:
                replacements[step.operation_index] = _clarify_missing_group_device(
                    step,
                    is_missing=step.operation_index in missing_device_indexes,
                )
            continue
        common_doctors = _common_doctors(ordered, context)
        anchor = _requested_group_anchor(ordered, context=context, timezone_name=timezone_name)
        if not common_doctors:
            explicit_doctors = {
                str(value)
                for step in ordered
                if (value := _params(step).get("doctor_id")) not in (None, "")
            }
            conflicting_explicit_doctors = (
                len(explicit_doctors) > 1
                and all(_params(step).get("doctor_id") not in (None, "") for step in ordered)
            )
            requested = (
                anchor[0]
                if anchor is not None
                else context.now.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
            )
            for step in ordered:
                if conflicting_explicit_doctors:
                    replacements[step.operation_index] = step.model_copy(
                        update={
                            "disposition": "clarify",
                            "reads": [],
                            "write_intent": None,
                            "state_action": "none",
                            "clarification_field": "doctor",
                            "response_goal": "ask_doctor_choice",
                            "facts": {
                                **step.facts,
                                "compound_visit_conflicting_doctors": True,
                            },
                        }
                    )
                else:
                    blocked = _suppress_group_write(step, requested_anchor=requested)
                    replacements[step.operation_index] = blocked.model_copy(
                        update={
                            "facts": {
                                **blocked.facts,
                                "compound_visit_no_common_doctor": True,
                            }
                        }
                    )
            continue
        if anchor is None:
            requested = context.now.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
            for step in ordered:
                replacements[step.operation_index] = _suppress_group_write(
                    step,
                    requested_anchor=requested,
                )
            continue

        requested_anchor, request_mode = anchor
        best: tuple[datetime, str, list[AvailabilitySlot], bool] | None = None
        for doctor_id in common_doctors:
            chain, exact_fits = _find_joint_chain(
                ordered,
                requested_anchor=requested_anchor,
                context=context,
                timezone_name=timezone_name,
                forced_doctor_id=doctor_id,
            )
            if chain is None:
                continue
            candidate = (chain[0].start_at, doctor_id, chain, exact_fits)
            if best is None or candidate[:2] < best[:2]:
                best = candidate

        if best is None:
            for step in ordered:
                replacements[step.operation_index] = _suppress_group_write(
                    step,
                    requested_anchor=requested_anchor,
                )
            continue

        _first_start, doctor_id, chain, exact_fits = best
        allow_write = request_mode == "next_available" or (request_mode == "exact" and exact_fits)
        group_id = visit_group_id or str(uuid4())
        for step, slot in zip(ordered, chain, strict=True):
            rewritten = _rewrite_step_for_slot(
                step,
                slot,
                timezone_name=timezone_name,
                requested_anchor=requested_anchor,
                allow_write=allow_write,
            )
            replacements[step.operation_index] = _attach_visit_group(
                rewritten,
                group_id,
                doctor_id,
            )

    if not replacements:
        return plan
    return plan.model_copy(
        update={"steps": [replacements.get(step.operation_index, step) for step in plan.steps]}
    )


def preflight_compound_visit_plan(
    plan: TurnPlan,
    *,
    context: ReadExecutionContext,
    timezone_name: str,
    visit_group_id: str | None = None,
) -> TurnPlan:
    """Resolve a same-visit booking group before any business write is allowed.

    If the customer's exact anchor can fit the full ordered visit, every booking is
    rewritten to the verified exact chain and normal per-step verification/writes may
    proceed. If the exact anchor cannot fit the whole group, no booking is allowed to
    write; the nearest joint window is returned as read-only availability instead.
    This prevents partial visits such as booking service A and only then discovering
    that service B cannot follow it.
    """
    plan = _auto_resolve_grouped_visits(
        plan,
        context=context,
        timezone_name=timezone_name,
        visit_group_id=visit_group_id,
    )
    groups: dict[str, list[PlanStep]] = {}
    for step in plan.steps:
        if _write_kind(step) != "booking":
            continue
        anchor = compound_anchor_key(step)
        sequence = compound_sequence_index(step)
        if anchor is None or sequence is None:
            continue
        groups.setdefault(anchor, []).append(step)
    if not groups:
        return plan

    replacements: dict[int, PlanStep] = {}
    for anchor_raw, group in groups.items():
        if len(group) < 2:
            continue
        try:
            requested_anchor = datetime.fromisoformat(anchor_raw)
        except ValueError:
            continue
        ordered = sorted(group, key=lambda step: compound_sequence_index(step) or 0)
        chain, exact_anchor_fits = _find_joint_chain(
            ordered,
            requested_anchor=requested_anchor,
            context=context,
            timezone_name=timezone_name,
        )
        if chain is None:
            for step in ordered:
                replacements[step.operation_index] = _suppress_group_write(
                    step,
                    requested_anchor=requested_anchor,
                )
            continue
        for step, slot in zip(ordered, chain, strict=True):
            replacements[step.operation_index] = _rewrite_step_for_slot(
                step,
                slot,
                timezone_name=timezone_name,
                requested_anchor=requested_anchor,
                allow_write=exact_anchor_fits,
            )

    if not replacements:
        return plan
    return plan.model_copy(
        update={
            "steps": [replacements.get(step.operation_index, step) for step in plan.steps],
        }
    )
