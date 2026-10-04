from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.v2.turn_contract import DateConstraint, TimeConstraint
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    DerivedRescheduleState,
    OptionChoice,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_rules import (
    apply_booking_date_change,
    apply_booking_device_change,
    apply_booking_doctor_change,
    apply_booking_package_usage_change,
    apply_booking_service_change,
    apply_booking_time_change,
    apply_reschedule_date_change,
    apply_reschedule_target_change,
    attach_option_snapshot,
    option_snapshot_is_current,
    preserve_task_for_side_read,
)

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def _booking_state() -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id="service-underarm",
            doctor_id="doctor-maryam",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-09-17", end_date=None),
            time=TimeConstraint(mode="after", start_time="18:00", end_time=None),
            package_usage="use_existing",
        ),
        derived=DerivedBookingState(
            availability_snapshot_id="availability-1",
            selected_slot_ref="slot-19",
            selected_package_id="package-1",
            package_validated=True,
            doctor_compatible=True,
            device_compatible=True,
            commercial_basis_presented_key="service-underarm|candela_gentle|package",
            commercial_basis_presented_device_key="candela_gentle",
        ),
        version=4,
    )


def _reschedule_state() -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=WriteAuthorization(
            operation="reschedule",
            authorized=True,
            source_turn_id="turn-2",
            granted_at=NOW,
        ),
        target=RescheduleTarget(
            appointment_id="appointment-old",
            service_id="service-underarm",
            doctor_id="doctor-maryam",
            device_key="candela_gentle",
            start_local="2026-09-17T19:00:00+03:00",
            payment_context={"billing": "package"},
        ),
        replacement=CustomerConstraints(
            service_id="service-underarm",
            doctor_id="doctor-maryam",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-09-19", end_date=None),
            time=TimeConstraint(mode="exact", start_time="19:00", end_time=None),
            package_usage="use_existing",
        ),
        derived=DerivedRescheduleState(
            availability_snapshot_id="replacement-1",
            selected_slot_ref="slot-new",
            package_validated=True,
            doctor_compatible=True,
            device_compatible=True,
        ),
        version=3,
    )


def test_service_change_preserves_customer_time_preferences_and_invalidates_dependents() -> None:
    state = _booking_state()
    changed = apply_booking_service_change(state, service_id="service-bikini")

    assert changed.version == state.version + 1
    assert changed.constraints.service_id == "service-bikini"
    assert changed.constraints.doctor_id == "doctor-maryam"
    assert changed.constraints.device_key == "candela_gentle"
    assert changed.constraints.date == state.constraints.date
    assert changed.constraints.time == state.constraints.time
    assert changed.constraints.package_usage == "use_existing"
    assert changed.derived.availability_snapshot_id is None
    assert changed.derived.selected_slot_ref is None
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False
    assert changed.derived.doctor_compatible is None
    assert changed.derived.device_compatible is None
    assert changed.derived.commercial_basis_presented_key is None


def test_doctor_and_time_changes_do_not_destroy_unrelated_customer_constraints() -> None:
    state = _booking_state()
    doctor_changed = apply_booking_doctor_change(state, doctor_id="doctor-sarah")
    assert doctor_changed.constraints.service_id == state.constraints.service_id
    assert doctor_changed.constraints.device_key == state.constraints.device_key
    assert doctor_changed.constraints.date == state.constraints.date
    assert doctor_changed.constraints.time == state.constraints.time
    assert doctor_changed.derived.selected_slot_ref is None
    assert doctor_changed.derived.selected_package_id == "package-1"

    time_changed = apply_booking_time_change(
        doctor_changed,
        time=TimeConstraint(mode="after", start_time="20:00", end_time=None),
    )
    assert time_changed.constraints.doctor_id == "doctor-sarah"
    assert time_changed.constraints.date == state.constraints.date
    assert time_changed.derived.selected_package_id == "package-1"


def test_device_change_invalidates_device_specific_package_selection() -> None:
    changed = apply_booking_device_change(_booking_state(), device_key="prime_lase")
    assert changed.constraints.device_key == "prime_lase"
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False
    assert changed.derived.availability_snapshot_id is None
    assert changed.derived.commercial_basis_presented_key is None


def test_date_change_revalidates_package_but_keeps_selected_package_identity() -> None:
    changed = apply_booking_date_change(
        _booking_state(),
        date=DateConstraint(mode="exact", start_date="2026-09-20", end_date=None),
    )
    assert changed.derived.selected_package_id == "package-1"
    assert changed.derived.package_validated is False
    assert changed.derived.selected_slot_ref is None


def test_package_usage_change_does_not_mutate_service_or_schedule_preferences() -> None:
    state = _booking_state()
    changed = apply_booking_package_usage_change(state, package_usage="avoid_existing")
    assert changed.constraints.service_id == state.constraints.service_id
    assert changed.constraints.date == state.constraints.date
    assert changed.constraints.time == state.constraints.time
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False
    assert changed.derived.commercial_basis_presented_key is None


def test_noop_change_and_side_read_do_not_advance_version() -> None:
    state = _booking_state()
    same = apply_booking_service_change(state, service_id=state.constraints.service_id)
    assert same is state
    assert preserve_task_for_side_read(state) is state
    assert state.version == 4


def test_option_snapshot_is_bound_to_task_version_and_expiry() -> None:
    state = attach_option_snapshot(
        _booking_state(),
        snapshot_id="snapshot-1",
        purpose="booking_slot",
        choices=[OptionChoice(ref="slot-1", label="7:00", payload={})],
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    assert option_snapshot_is_current(state, now=NOW + timedelta(minutes=5)) is True
    assert option_snapshot_is_current(state, now=NOW + timedelta(minutes=11)) is False

    changed = apply_booking_doctor_change(state, doctor_id="doctor-sarah")
    assert changed.option_snapshot is None
    assert option_snapshot_is_current(changed, now=NOW + timedelta(minutes=5)) is False


def test_reschedule_target_change_rebuilds_replacement_from_new_verified_target() -> None:
    state = _reschedule_state()
    new_target = RescheduleTarget(
        appointment_id="appointment-other",
        service_id="service-hydrafacial",
        doctor_id="doctor-sarah",
        device_key=None,
        start_local="2026-09-18T17:00:00+03:00",
        payment_context={"billing": "standard"},
    )
    changed = apply_reschedule_target_change(state, target=new_target)

    assert changed.target == new_target
    assert changed.replacement.service_id == "service-hydrafacial"
    assert changed.replacement.doctor_id == "doctor-sarah"
    assert changed.replacement.device_key is None
    assert changed.replacement.date is None
    assert changed.replacement.time is None
    assert changed.replacement.package_usage == "unspecified"
    assert changed.derived == DerivedRescheduleState()
    assert changed.option_snapshot is None


def test_reschedule_date_change_preserves_replacement_identity_and_time_preference() -> None:
    state = _reschedule_state()
    changed = apply_reschedule_date_change(
        state,
        date=DateConstraint(mode="exact", start_date="2026-09-21", end_date=None),
    )
    assert changed.replacement.service_id == state.replacement.service_id
    assert changed.replacement.doctor_id == state.replacement.doctor_id
    assert changed.replacement.device_key == state.replacement.device_key
    assert changed.replacement.time == state.replacement.time
    assert changed.derived.availability_snapshot_id is None
    assert changed.derived.selected_slot_ref is None
    assert changed.derived.package_validated is False


def test_package_usage_change_does_not_create_pulse_billing_preference() -> None:
    state = _booking_state()
    changed = apply_booking_package_usage_change(state, package_usage="avoid_existing")

    assert changed.constraints.package_usage == "avoid_existing"
    assert changed.constraints.pulse_usage == "unspecified"
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False
    assert changed.option_snapshot is None
