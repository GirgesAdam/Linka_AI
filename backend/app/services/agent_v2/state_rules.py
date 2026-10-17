from __future__ import annotations

from datetime import datetime

from app.agents.v2.turn_contract import ClockSource, DateConstraint, PackageUsage, TimeConstraint
from app.services.agent_v2.state import (
    ActiveTaskState,
    BookingProposal,
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    DerivedRescheduleState,
    OptionChoice,
    OptionSnapshot,
    RescheduleTarget,
    RescheduleTaskState,
)


def _booking_derived(
    state: BookingTaskState,
    *,
    clear_availability: bool = False,
    clear_package: bool = False,
    reset_doctor_compatibility: bool = False,
    reset_device_compatibility: bool = False,
    clear_commercial_basis: bool = False,
) -> DerivedBookingState:
    update: dict[str, object] = {}
    if clear_availability:
        update.update(
            availability_snapshot_id=None,
            selected_slot_ref=None,
        )
    if clear_package:
        update.update(
            selected_package_id=None,
            package_validated=False,
        )
    if reset_doctor_compatibility:
        update["doctor_compatible"] = None
    if reset_device_compatibility:
        update["device_compatible"] = None
    if clear_commercial_basis:
        update["commercial_basis_presented_key"] = None
        update["commercial_basis_presented_device_key"] = None
    return state.derived.model_copy(update=update)


def _reschedule_derived(
    state: RescheduleTaskState,
    *,
    clear_availability: bool = False,
    reset_package_validation: bool = False,
    reset_doctor_compatibility: bool = False,
    reset_device_compatibility: bool = False,
) -> DerivedRescheduleState:
    update: dict[str, object] = {}
    if clear_availability:
        update.update(
            availability_snapshot_id=None,
            selected_slot_ref=None,
        )
    if reset_package_validation:
        update["package_validated"] = False
    if reset_doctor_compatibility:
        update["doctor_compatible"] = None
    if reset_device_compatibility:
        update["device_compatible"] = None
    return state.derived.model_copy(update=update)


def _next_booking_state(
    state: BookingTaskState,
    *,
    constraints: CustomerConstraints,
    derived: DerivedBookingState,
) -> BookingTaskState:
    return state.model_copy(
        update={
            "status": "collecting",
            "constraints": constraints,
            "derived": derived,
            "proposal": None,
            "option_snapshot": None,
            "version": state.version + 1,
        }
    )


def _next_reschedule_state(
    state: RescheduleTaskState,
    *,
    replacement: CustomerConstraints,
    derived: DerivedRescheduleState,
) -> RescheduleTaskState:
    return state.model_copy(
        update={
            "status": "collecting",
            "replacement": replacement,
            "derived": derived,
            "option_snapshot": None,
            "version": state.version + 1,
        }
    )


def apply_booking_service_change(
    state: BookingTaskState,
    *,
    service_id: str | None,
) -> BookingTaskState:
    if state.constraints.service_id == service_id:
        return state
    constraints = state.constraints.model_copy(update={"service_id": service_id})
    derived = _booking_derived(
        state,
        clear_availability=True,
        clear_package=True,
        reset_doctor_compatibility=True,
        reset_device_compatibility=True,
        clear_commercial_basis=True,
    )
    return _next_booking_state(state, constraints=constraints, derived=derived)


def apply_booking_doctor_change(
    state: BookingTaskState,
    *,
    doctor_id: str | None,
) -> BookingTaskState:
    if state.constraints.doctor_id == doctor_id:
        return state
    constraints = state.constraints.model_copy(update={"doctor_id": doctor_id})
    derived = _booking_derived(
        state,
        clear_availability=True,
        reset_doctor_compatibility=True,
    )
    return _next_booking_state(state, constraints=constraints, derived=derived)


def apply_booking_branch_change(
    state: BookingTaskState,
    *,
    branch_id: str | None,
) -> BookingTaskState:
    if state.constraints.branch_id == branch_id:
        return state
    constraints = state.constraints.model_copy(update={"branch_id": branch_id})
    derived = _booking_derived(state, clear_availability=True)
    return _next_booking_state(state, constraints=constraints, derived=derived)


def apply_booking_device_change(
    state: BookingTaskState,
    *,
    device_key: str | None,
) -> BookingTaskState:
    if state.constraints.device_key == device_key:
        return state
    constraints = state.constraints.model_copy(update={"device_key": device_key})
    derived = _booking_derived(
        state,
        clear_availability=True,
        clear_package=True,
        reset_device_compatibility=True,
        clear_commercial_basis=True,
    )
    return _next_booking_state(state, constraints=constraints, derived=derived)


def apply_booking_date_change(
    state: BookingTaskState,
    *,
    date: DateConstraint | None,
) -> BookingTaskState:
    if state.constraints.date == date:
        return state
    constraints = state.constraints.model_copy(update={"date": date})
    derived = _booking_derived(state, clear_availability=True)
    derived = derived.model_copy(update={"package_validated": False})
    return _next_booking_state(state, constraints=constraints, derived=derived)


def apply_booking_time_change(
    state: BookingTaskState,
    *,
    time: TimeConstraint | None,
    clock_source: ClockSource | None = None,
) -> BookingTaskState:
    source = (
        "unspecified"
        if time is None
        else clock_source
        if clock_source is not None
        else state.constraints.time_clock_source
    )
    if state.constraints.time == time and state.constraints.time_clock_source == source:
        return state
    constraints = state.constraints.model_copy(
        update={"time": time, "time_clock_source": source}
    )
    derived = _booking_derived(state, clear_availability=True)
    return _next_booking_state(state, constraints=constraints, derived=derived)


def apply_booking_package_usage_change(
    state: BookingTaskState,
    *,
    package_usage: PackageUsage,
) -> BookingTaskState:
    if state.constraints.package_usage == package_usage:
        return state
    constraints = state.constraints.model_copy(update={"package_usage": package_usage})
    derived = _booking_derived(state, clear_package=True, clear_commercial_basis=True)
    return _next_booking_state(state, constraints=constraints, derived=derived)


def apply_booking_requested_services_change(
    state: BookingTaskState,
    *,
    service_ids: list[str],
) -> BookingTaskState:
    ordered = list(dict.fromkeys(str(item) for item in service_ids if str(item)))
    if state.requested_service_ids == ordered:
        return state
    retained_devices = {
        service_id: device_key
        for service_id, device_key in state.service_device_keys.items()
        if service_id in ordered
    }
    if state.constraints.service_id in ordered and state.constraints.device_key:
        retained_devices[state.constraints.service_id] = state.constraints.device_key
    return state.model_copy(
        update={
            "status": "collecting",
            "requested_service_ids": ordered,
            "service_device_keys": retained_devices,
            "proposal": None,
            "grouped": None,
            "derived": _booking_derived(
                state,
                clear_availability=True,
                clear_package=True,
                reset_doctor_compatibility=True,
                reset_device_compatibility=True,
                clear_commercial_basis=True,
            ),
            "option_snapshot": None,
            "version": state.version + 1,
        }
    )


def attach_booking_proposal(
    state: BookingTaskState,
    *,
    proposal: BookingProposal,
) -> BookingTaskState:
    next_version = state.version + 1
    current = proposal.model_copy(update={"task_version": next_version})
    return state.model_copy(
        update={
            "status": "awaiting_confirmation",
            "proposal": current,
            "option_snapshot": None,
            "version": next_version,
        }
    )


def invalidate_booking_proposal(state: BookingTaskState) -> BookingTaskState:
    if state.proposal is None:
        return state
    return state.model_copy(
        update={
            "status": "collecting",
            "proposal": None,
            "option_snapshot": None,
            "version": state.version + 1,
        }
    )


def booking_proposal_is_current(
    state: BookingTaskState,
    *,
    proposal_id: str,
    now: datetime,
) -> bool:
    proposal = state.proposal
    return (
        proposal is not None
        and proposal.proposal_id == proposal_id
        and proposal.is_current(task_version=state.version, now=now)
    )


def record_booking_commercial_basis_presented(
    state: BookingTaskState,
    *,
    commercial_key: str,
) -> BookingTaskState:
    if state.derived.commercial_basis_presented_key == commercial_key:
        return state
    legacy_device = state.constraints.device_key
    return state.model_copy(
        update={
            "status": "collecting",
            "derived": state.derived.model_copy(
                update={
                    "commercial_basis_presented_key": commercial_key,
                    "commercial_basis_presented_device_key": legacy_device,
                }
            ),
            "version": state.version + 1,
            "proposal": None,
            "option_snapshot": None,
        }
    )


def record_booking_doctor_compatibility(
    state: BookingTaskState,
    *,
    compatible: bool,
) -> BookingTaskState:
    if state.derived.doctor_compatible is compatible:
        return state
    return state.model_copy(
        update={
            "status": "collecting",
            "derived": state.derived.model_copy(update={"doctor_compatible": compatible}),
            "version": state.version + 1,
            "proposal": None,
            "option_snapshot": None,
        }
    )


def record_booking_device_compatibility(
    state: BookingTaskState,
    *,
    compatible: bool,
) -> BookingTaskState:
    if state.derived.device_compatible is compatible:
        return state
    return state.model_copy(
        update={
            "status": "collecting",
            "derived": state.derived.model_copy(update={"device_compatible": compatible}),
            "version": state.version + 1,
            "proposal": None,
            "option_snapshot": None,
        }
    )


def restore_booking_slot_snapshot(
    state: BookingTaskState,
    *,
    snapshot: OptionSnapshot,
) -> BookingTaskState:
    """Restore a verified narrowed booking-slot snapshot without another version bump."""
    return state.model_copy(
        update={
            "status": "awaiting_slot",
            "proposal": None,
            "option_snapshot": snapshot,
            "derived": state.derived.model_copy(
                update={
                    "availability_snapshot_id": snapshot.snapshot_id,
                    "selected_slot_ref": None,
                }
            ),
        }
    )

def invalidate_booking_slot_selection(state: BookingTaskState) -> BookingTaskState:
    """Clear stale booking-slot authority after fresh verification rejects a selection."""
    if (
        state.status == "collecting"
        and state.option_snapshot is None
        and state.proposal is None
        and state.derived.availability_snapshot_id is None
        and state.derived.selected_slot_ref is None
    ):
        return state
    return state.model_copy(
        update={
            "status": "collecting",
            "proposal": None,
            "option_snapshot": None,
            "derived": _booking_derived(state, clear_availability=True),
            "version": state.version + 1,
        }
    )


def apply_verified_booking_slot_selection(
    state: BookingTaskState,
    *,
    selected_slot_ref: str,
    main_service_id: str,
    branch_id: str,
    doctor_id: str,
    device_key: str | None,
    date: DateConstraint,
    time: TimeConstraint,
    service_device_keys: dict[str, str],
) -> BookingTaskState:
    """Materialize one freshly verified slot before attaching its proposal."""
    constraints = state.constraints.model_copy(
        update={
            "service_id": main_service_id,
            "branch_id": branch_id,
            "doctor_id": doctor_id,
            "device_key": device_key,
            "date": date,
            "time": time,
        }
    )
    return state.model_copy(
        update={
            "status": "collecting",
            "constraints": constraints,
            "service_device_keys": dict(service_device_keys),
            "proposal": None,
            "option_snapshot": None,
            "derived": state.derived.model_copy(
                update={
                    "availability_snapshot_id": None,
                    "selected_slot_ref": selected_slot_ref,
                }
            ),
            "version": state.version + 1,
        }
    )


def record_booking_selected_slot(
    state: BookingTaskState,
    *,
    selected_slot_ref: str,
) -> BookingTaskState:
    """Record verified booking-slot identity without advancing stage or version."""
    if state.derived.selected_slot_ref == selected_slot_ref:
        return state
    return state.model_copy(
        update={
            "derived": state.derived.model_copy(
                update={"selected_slot_ref": selected_slot_ref}
            )
        }
    )


def attach_option_snapshot(
    state: ActiveTaskState,
    *,
    snapshot_id: str,
    purpose: str,
    choices: list[OptionChoice],
    created_at: datetime,
    expires_at: datetime,
) -> ActiveTaskState:
    next_version = state.version + 1
    snapshot = OptionSnapshot(
        snapshot_id=snapshot_id,
        purpose=purpose,
        task_version=next_version,
        created_at=created_at,
        expires_at=expires_at,
        options=choices,
    )
    if isinstance(state, BookingTaskState):
        status = "awaiting_slot" if snapshot.purpose == "booking_slot" else "collecting"
    else:
        status = "awaiting_choice"
    update: dict[str, object] = {
        "status": status,
        "option_snapshot": snapshot,
        "version": next_version,
    }
    if isinstance(state, BookingTaskState):
        update["proposal"] = None
        if snapshot.purpose == "booking_slot":
            update["derived"] = state.derived.model_copy(
                update={
                    "availability_snapshot_id": snapshot_id,
                    "selected_slot_ref": None,
                }
            )
    return state.model_copy(update=update)


def option_snapshot_is_current(
    state: ActiveTaskState,
    *,
    now: datetime,
) -> bool:
    snapshot = state.option_snapshot
    return snapshot is not None and snapshot.is_current(task_version=state.version, now=now)


def preserve_task_for_side_read(state: ActiveTaskState) -> ActiveTaskState:
    """An informational side operation must not mutate operational workflow state."""
    return state


def apply_reschedule_target_change(
    state: RescheduleTaskState,
    *,
    target: RescheduleTarget,
) -> RescheduleTaskState:
    if state.target == target:
        return state
    replacement = CustomerConstraints(
        service_id=target.service_id,
        doctor_id=target.doctor_id,
        device_key=target.device_key,
        date=None,
        time=None,
        package_usage="unspecified",
        pulse_usage="unspecified",
    )
    return state.model_copy(
        update={
            "status": "collecting",
            "target": target,
            "replacement": replacement,
            "derived": DerivedRescheduleState(),
            "option_snapshot": None,
            "version": state.version + 1,
        }
    )


def apply_reschedule_service_change(
    state: RescheduleTaskState,
    *,
    service_id: str | None,
) -> RescheduleTaskState:
    if state.replacement.service_id == service_id:
        return state
    replacement = state.replacement.model_copy(update={"service_id": service_id})
    derived = _reschedule_derived(
        state,
        clear_availability=True,
        reset_package_validation=True,
        reset_doctor_compatibility=True,
        reset_device_compatibility=True,
    )
    return _next_reschedule_state(state, replacement=replacement, derived=derived)


def apply_reschedule_doctor_change(
    state: RescheduleTaskState,
    *,
    doctor_id: str | None,
) -> RescheduleTaskState:
    if state.replacement.doctor_id == doctor_id:
        return state
    replacement = state.replacement.model_copy(update={"doctor_id": doctor_id})
    derived = _reschedule_derived(
        state,
        clear_availability=True,
        reset_doctor_compatibility=True,
    )
    return _next_reschedule_state(state, replacement=replacement, derived=derived)


def apply_reschedule_device_change(
    state: RescheduleTaskState,
    *,
    device_key: str | None,
) -> RescheduleTaskState:
    if state.replacement.device_key == device_key:
        return state
    replacement = state.replacement.model_copy(update={"device_key": device_key})
    derived = _reschedule_derived(
        state,
        clear_availability=True,
        reset_package_validation=True,
        reset_device_compatibility=True,
    )
    return _next_reschedule_state(state, replacement=replacement, derived=derived)


def apply_reschedule_date_change(
    state: RescheduleTaskState,
    *,
    date: DateConstraint | None,
) -> RescheduleTaskState:
    if state.replacement.date == date:
        return state
    replacement = state.replacement.model_copy(update={"date": date})
    derived = _reschedule_derived(
        state,
        clear_availability=True,
        reset_package_validation=True,
    )
    return _next_reschedule_state(state, replacement=replacement, derived=derived)


def apply_reschedule_branch_change(
    state: RescheduleTaskState,
    *,
    branch_id: str | None,
) -> RescheduleTaskState:
    if state.replacement.branch_id == branch_id:
        return state
    replacement = state.replacement.model_copy(update={"branch_id": branch_id})
    derived = _reschedule_derived(state, clear_availability=True)
    return _next_reschedule_state(state, replacement=replacement, derived=derived)


def apply_reschedule_time_change(
    state: RescheduleTaskState,
    *,
    time: TimeConstraint | None,
    clock_source: ClockSource | None = None,
) -> RescheduleTaskState:
    source = (
        "unspecified"
        if time is None
        else clock_source
        if clock_source is not None
        else state.replacement.time_clock_source
    )
    if state.replacement.time == time and state.replacement.time_clock_source == source:
        return state
    replacement = state.replacement.model_copy(
        update={"time": time, "time_clock_source": source}
    )
    derived = _reschedule_derived(state, clear_availability=True)
    return _next_reschedule_state(state, replacement=replacement, derived=derived)
