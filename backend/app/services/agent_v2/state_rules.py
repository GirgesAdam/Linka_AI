from __future__ import annotations

from datetime import datetime

from app.agents.v2.turn_contract import DateConstraint, PackageUsage, TimeConstraint
from app.services.agent_v2.state import (
    ActiveTaskState,
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
) -> BookingTaskState:
    if state.constraints.time == time:
        return state
    constraints = state.constraints.model_copy(update={"time": time})
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


def record_booking_commercial_basis_presented(
    state: BookingTaskState,
    *,
    device_key: str,
) -> BookingTaskState:
    if state.derived.commercial_basis_presented_device_key == device_key:
        return state
    return state.model_copy(
        update={
            "derived": state.derived.model_copy(
                update={"commercial_basis_presented_device_key": device_key}
            ),
            "version": state.version + 1,
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
            "derived": state.derived.model_copy(update={"doctor_compatible": compatible}),
            "version": state.version + 1,
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
            "derived": state.derived.model_copy(update={"device_compatible": compatible}),
            "version": state.version + 1,
            "option_snapshot": None,
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
    return state.model_copy(
        update={
            "status": "awaiting_choice",
            "option_snapshot": snapshot,
            "version": next_version,
        }
    )


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


def apply_reschedule_time_change(
    state: RescheduleTaskState,
    *,
    time: TimeConstraint | None,
) -> RescheduleTaskState:
    if state.replacement.time == time:
        return state
    replacement = state.replacement.model_copy(update={"time": time})
    derived = _reschedule_derived(state, clear_availability=True)
    return _next_reschedule_state(state, replacement=replacement, derived=derived)
