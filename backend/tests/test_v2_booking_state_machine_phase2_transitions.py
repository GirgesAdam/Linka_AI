from datetime import UTC, datetime, timedelta

import pytest

from app.agents.v2.turn_contract import DateConstraint, TimeConstraint
from app.services.agent_v2.state import (
    BookingProposal,
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_rules import (
    apply_booking_branch_change,
    apply_booking_date_change,
    apply_booking_device_change,
    apply_booking_doctor_change,
    apply_booking_package_usage_change,
    apply_booking_requested_services_change,
    apply_booking_service_change,
    apply_booking_time_change,
    attach_option_snapshot,
    preserve_task_for_side_read,
    record_booking_selected_slot,
)

NOW = datetime(2026, 10, 16, 17, 0, tzinfo=UTC)
DATE = DateConstraint(mode="exact", start_date="2026-10-18")
TIME = TimeConstraint(mode="exact", start_time="20:00")


def _proposal(*, task_version: int) -> BookingProposal:
    start = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
    return BookingProposal(
        proposal_id="proposal-1",
        patient_id="patient-1",
        service_ids=["svc-a"],
        main_service_id="svc-a",
        branch_id="branch-1",
        doctor_id="doc-a",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        device_key="candela",
        task_version=task_version,
        proposal_version=1,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=14),
    )


def _constraints() -> CustomerConstraints:
    return CustomerConstraints(
        service_id="svc-a",
        doctor_id="doc-a",
        branch_id="branch-1",
        device_key="candela",
        date=DATE,
        time=TIME,
        package_usage="use_existing",
    )


def _derived() -> DerivedBookingState:
    return DerivedBookingState(
        availability_snapshot_id="snapshot-old",
        selected_slot_ref="slot-old",
        selected_package_id="package-1",
        package_validated=True,
        doctor_compatible=True,
        device_compatible=True,
        commercial_basis_presented_key="svc-a|candela|package",
        commercial_basis_presented_device_key="candela",
    )


def _slot_snapshot(*, task_version: int) -> OptionSnapshot:
    return OptionSnapshot(
        snapshot_id="snapshot-old",
        purpose="booking_slot",
        task_version=task_version,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=14),
        options=[OptionChoice(ref="slot-old", payload={"start_time_24h": "20:00"})],
    )


def _state(stage: str) -> BookingTaskState:
    common = dict(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=_constraints(),
        requested_service_ids=["svc-a"],
        service_device_keys={"svc-a": "candela"},
        derived=_derived(),
        version=4,
    )
    if stage == "collecting":
        return BookingTaskState(status="collecting", **common)
    if stage == "awaiting_slot":
        return BookingTaskState(
            status="awaiting_slot",
            option_snapshot=_slot_snapshot(task_version=4),
            **common,
        )
    if stage == "awaiting_confirmation":
        return BookingTaskState(
            status="awaiting_confirmation",
            proposal=_proposal(task_version=4),
            **common,
        )
    raise AssertionError(f"unexpected stage: {stage}")


def _apply_change(state: BookingTaskState, change: str) -> BookingTaskState:
    if change == "service":
        return apply_booking_service_change(state, service_id="svc-b")
    if change == "doctor":
        return apply_booking_doctor_change(state, doctor_id="doc-b")
    if change == "device":
        return apply_booking_device_change(state, device_key="prime")
    if change == "branch":
        return apply_booking_branch_change(state, branch_id="branch-2")
    if change == "date":
        return apply_booking_date_change(
            state, date=DateConstraint(mode="exact", start_date="2026-10-19")
        )
    if change == "time":
        return apply_booking_time_change(
            state, time=TimeConstraint(mode="exact", start_time="21:00")
        )
    if change == "package":
        return apply_booking_package_usage_change(state, package_usage="avoid_existing")
    if change == "services":
        return apply_booking_requested_services_change(
            state, service_ids=["svc-a", "svc-b"]
        )
    raise AssertionError(f"unexpected change: {change}")


@pytest.mark.parametrize(
    ("stage", "change"),
    [
        ("collecting", "service"),
        ("awaiting_confirmation", "doctor"),
        ("awaiting_slot", "device"),
        ("awaiting_slot", "branch"),
        ("awaiting_slot", "date"),
        ("awaiting_confirmation", "time"),
        ("awaiting_confirmation", "package"),
        ("awaiting_slot", "services"),
    ],
)
def test_meaningful_booking_changes_return_to_collecting(stage: str, change: str) -> None:
    state = _state(stage)

    changed = _apply_change(state, change)

    assert changed.status == "collecting"
    assert changed.version == state.version + 1
    assert changed.proposal is None
    assert changed.option_snapshot is None


def test_time_correction_clears_proposal_and_stale_slot_authority() -> None:
    state = _state("awaiting_confirmation")

    changed = apply_booking_time_change(
        state,
        time=TimeConstraint(mode="exact", start_time="21:00"),
    )

    assert changed.status == "collecting"
    assert changed.proposal is None
    assert changed.constraints.time == TimeConstraint(mode="exact", start_time="21:00")
    assert changed.derived.availability_snapshot_id is None
    assert changed.derived.selected_slot_ref is None


def test_date_correction_clears_booking_slot_snapshot_and_selection() -> None:
    state = _state("awaiting_slot")

    changed = apply_booking_date_change(
        state,
        date=DateConstraint(mode="exact", start_date="2026-10-19"),
    )

    assert changed.status == "collecting"
    assert changed.option_snapshot is None
    assert changed.constraints.date == DateConstraint(mode="exact", start_date="2026-10-19")
    assert changed.derived.availability_snapshot_id is None
    assert changed.derived.selected_slot_ref is None


def test_doctor_correction_clears_proposal_and_slot_authority() -> None:
    state = _state("awaiting_confirmation")

    changed = apply_booking_doctor_change(state, doctor_id="doc-b")

    assert changed.status == "collecting"
    assert changed.proposal is None
    assert changed.constraints.doctor_id == "doc-b"
    assert changed.derived.availability_snapshot_id is None
    assert changed.derived.selected_slot_ref is None
    assert changed.derived.doctor_compatible is None
    assert changed.derived.selected_package_id == "package-1"


def test_package_change_preserves_schedule_resources_and_slot_identity() -> None:
    state = _state("awaiting_confirmation")

    changed = apply_booking_package_usage_change(state, package_usage="avoid_existing")

    assert changed.status == "collecting"
    assert changed.proposal is None
    assert changed.constraints.service_id == state.constraints.service_id
    assert changed.constraints.doctor_id == state.constraints.doctor_id
    assert changed.constraints.branch_id == state.constraints.branch_id
    assert changed.constraints.device_key == state.constraints.device_key
    assert changed.constraints.date == state.constraints.date
    assert changed.constraints.time == state.constraints.time
    assert changed.derived.availability_snapshot_id == "snapshot-old"
    assert changed.derived.selected_slot_ref == "slot-old"
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False
    assert changed.derived.commercial_basis_presented_key is None


@pytest.mark.parametrize("change", ["service", "doctor", "device", "date", "time", "package", "services"])
def test_noop_booking_changes_preserve_confirmation_state(change: str) -> None:
    state = _state("awaiting_confirmation")
    if change == "service":
        same = apply_booking_service_change(state, service_id="svc-a")
    elif change == "doctor":
        same = apply_booking_doctor_change(state, doctor_id="doc-a")
    elif change == "device":
        same = apply_booking_device_change(state, device_key="candela")
    elif change == "date":
        same = apply_booking_date_change(state, date=DATE)
    elif change == "time":
        same = apply_booking_time_change(state, time=TIME)
    elif change == "package":
        same = apply_booking_package_usage_change(state, package_usage="use_existing")
    else:
        same = apply_booking_requested_services_change(state, service_ids=["svc-a"])

    assert same is state
    assert same.status == "awaiting_confirmation"
    assert same.proposal is state.proposal
    assert same.version == 4


def test_side_read_preserves_confirmation_task_exactly() -> None:
    state = _state("awaiting_confirmation")

    preserved = preserve_task_for_side_read(state)

    assert preserved is state
    assert preserved.status == "awaiting_confirmation"
    assert preserved.proposal is state.proposal
    assert preserved.version == state.version


def test_booking_slot_snapshot_and_selected_ref_have_canonical_state_rule_owners() -> None:
    state = _state("collecting")

    with_snapshot = attach_option_snapshot(
        state,
        snapshot_id="snapshot-new",
        purpose="booking_slot",
        choices=[OptionChoice(ref="slot-new", payload={"start_time_24h": "21:00"})],
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    assert isinstance(with_snapshot, BookingTaskState)
    assert with_snapshot.status == "awaiting_slot"
    assert with_snapshot.derived.availability_snapshot_id == "snapshot-new"
    assert with_snapshot.version == state.version + 1

    selected = record_booking_selected_slot(
        with_snapshot,
        selected_slot_ref="slot-new",
    )
    assert selected.derived.selected_slot_ref == "slot-new"
    assert selected.status == "awaiting_slot"
    assert selected.version == with_snapshot.version