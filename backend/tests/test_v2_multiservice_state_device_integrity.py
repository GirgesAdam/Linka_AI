from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context
from app.services.agent_v2.active_task_progress import plan_active_task_progress
from app.services.agent_v2.multi_service_booking import (
    common_laser_device_keys,
    select_main_service_id,
)
from app.services.agent_v2.state import BookingTaskState, CustomerConstraints, WriteAuthorization
from app.services.agent_v2.state_executor import reconcile_booking_service_set_state
from app.services.agent_v2.state_rules import apply_booking_requested_services_change

NOW = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)
FULL_LEGS = "full-legs"
FULL_ARMS = "full-arms"
UNDERARM = "underarm"
COMBO = "combo"
BIKINI = "bikini"
NON_LASER = "consult"
CANDELA = "candela"
DEKA = "deka"


def _device(key: str) -> dict[str, object]:
    return {
        "device_key": key,
        "device_name": key.title(),
        "configured": True,
        "price_minor": 100_000,
        "duration_minutes": 30,
    }


def _catalog(*, arms_devices: list[str] | None = None) -> dict[str, object]:
    arms = arms_devices if arms_devices is not None else [CANDELA, DEKA]
    return {
        "services": [
            {
                "id": FULL_LEGS,
                "name": "Full Legs",
                "duration_minutes": 60,
                "requires_laser_device": True,
                "laser_devices": [_device(CANDELA), _device(DEKA)],
            },
            {
                "id": FULL_ARMS,
                "name": "Full Arms",
                "duration_minutes": 40,
                "requires_laser_device": True,
                "laser_devices": [_device(key) for key in arms],
            },
            {
                "id": UNDERARM,
                "name": "Underarm",
                "duration_minutes": 20,
                "requires_laser_device": True,
                "laser_devices": [_device(CANDELA), _device(DEKA)],
            },
            {
                "id": BIKINI,
                "name": "Bikini",
                "duration_minutes": 25,
                "requires_laser_device": True,
                "laser_devices": [_device(CANDELA), _device(DEKA)],
            },
            {
                "id": COMBO,
                "name": "Underarm + Bikini",
                "duration_minutes": 45,
                "requires_laser_device": True,
                "laser_devices": [_device(CANDELA), _device(DEKA)],
            },
            {
                "id": NON_LASER,
                "name": "Consult",
                "duration_minutes": 10,
                "requires_laser_device": False,
            },
        ],
        "branches": [{"id": "branch-1", "name": "Main"}],
        "doctors": [],
    }


def _task(
    services: list[str],
    *,
    main: str,
    device: str | None = None,
) -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id=main,
            device_key=device,
            branch_id="branch-1",
        ),
        requested_service_ids=list(services),
        service_device_keys=(
            {service_id: device for service_id in services if device is not None}
            if device is not None
            else {}
        ),
    )


def _mutate_services(
    state: BookingTaskState,
    services: list[str],
    *,
    catalog: dict[str, object] | None = None,
) -> BookingTaskState:
    changed = apply_booking_requested_services_change(state, service_ids=services)
    return reconcile_booking_service_set_state(changed, catalog=catalog or _catalog())


def test_m1_original_s13_recomputes_main_from_current_requested_set() -> None:
    state = _task([FULL_LEGS, FULL_ARMS, UNDERARM], main=FULL_LEGS, device=CANDELA)
    updated = _mutate_services(state, [FULL_ARMS, UNDERARM])

    assert updated.requested_service_ids == [FULL_ARMS, UNDERARM]
    assert updated.constraints.service_id == FULL_ARMS
    assert FULL_LEGS not in updated.requested_service_ids
    assert FULL_LEGS not in updated.service_device_keys
    assert updated.service_device_keys == {FULL_ARMS: CANDELA, UNDERARM: CANDELA}
    assert updated.proposal is None
    assert updated.option_snapshot is None


def test_m2_removing_additional_keeps_current_longest_main() -> None:
    state = _task([FULL_LEGS, FULL_ARMS, UNDERARM], main=FULL_LEGS, device=CANDELA)
    updated = _mutate_services(state, [FULL_LEGS, FULL_ARMS])

    assert updated.requested_service_ids == [FULL_LEGS, FULL_ARMS]
    assert updated.constraints.service_id == FULL_LEGS


def test_m3_adding_longer_service_recomputes_main() -> None:
    state = _task([UNDERARM, FULL_ARMS], main=FULL_ARMS, device=CANDELA)
    updated = _mutate_services(state, [UNDERARM, FULL_ARMS, FULL_LEGS])

    assert updated.constraints.service_id == FULL_LEGS
    assert updated.requested_service_ids == [UNDERARM, FULL_ARMS, FULL_LEGS]


def test_m4_two_to_one_collapse_leaves_combo_as_only_main() -> None:
    state = _task([COMBO, FULL_ARMS], main=COMBO, device=CANDELA)
    updated = _mutate_services(state, [COMBO])

    assert updated.requested_service_ids == [COMBO]
    assert updated.constraints.service_id == COMBO
    assert updated.service_device_keys == {COMBO: CANDELA}


def test_m5_combo_canonical_set_does_not_resurrect_covered_atomic_services() -> None:
    state = _task([COMBO, FULL_ARMS], main=COMBO, device=CANDELA)
    updated = _mutate_services(state, [COMBO])

    assert updated.requested_service_ids == [COMBO]
    assert UNDERARM not in updated.requested_service_ids
    assert BIKINI not in updated.requested_service_ids
    assert select_main_service_id(_catalog(), updated.requested_service_ids) == COMBO


def test_d3_common_device_intersection_uses_only_shared_device() -> None:
    catalog = _catalog(arms_devices=[CANDELA])
    assert common_laser_device_keys(catalog, [UNDERARM, FULL_ARMS]) == [CANDELA]


def test_d4_multiple_common_devices_require_existing_device_choice_step() -> None:
    catalog = _catalog(arms_devices=[CANDELA, DEKA])
    state = _task([UNDERARM, FULL_ARMS], main=FULL_ARMS, device=None)
    context = build_semantic_context(catalog)

    step = plan_active_task_progress(state, operation_index=0, context=context)

    assert step.disposition == "read"
    assert step.facts["booking_device_price_step"] is True
    assert step.facts["compatible_visit_device_keys"] == [CANDELA, DEKA]
    assert step.facts.get("device_key") is None
    assert len(step.reads) == 1
    assert step.reads[0].kind == "service_catalog_set"
    assert step.reads[0].parameters["service_ids"] == [UNDERARM, FULL_ARMS]


def test_d5_zero_common_device_fails_closed_before_availability_or_write() -> None:
    catalog = _catalog(arms_devices=[DEKA])
    catalog["services"][2]["laser_devices"] = [_device(CANDELA)]  # Underarm.
    state = _task([UNDERARM, FULL_ARMS], main=FULL_ARMS, device=None)
    context = build_semantic_context(catalog)

    step = plan_active_task_progress(state, operation_index=0, context=context)

    assert common_laser_device_keys(catalog, [UNDERARM, FULL_ARMS]) == []
    assert step.disposition == "clarify"
    assert step.clarification_field == "device"
    assert step.facts["multi_service_no_common_device"] is True
    assert step.reads == []
    assert step.write_intent is None


def test_d7_remove_service_keeps_visit_device_when_still_common() -> None:
    state = _task([FULL_LEGS, FULL_ARMS, UNDERARM], main=FULL_LEGS, device=CANDELA)
    updated = _mutate_services(state, [FULL_ARMS, UNDERARM])

    assert updated.constraints.device_key == CANDELA
    assert updated.service_device_keys == {FULL_ARMS: CANDELA, UNDERARM: CANDELA}


def test_d8_add_service_invalidates_device_when_it_is_not_common_anymore() -> None:
    catalog = _catalog(arms_devices=[DEKA])
    state = _task([UNDERARM], main=UNDERARM, device=CANDELA)
    updated = _mutate_services(state, [UNDERARM, FULL_ARMS], catalog=catalog)

    assert updated.constraints.service_id == FULL_ARMS
    # Candela is invalidated, then the only common visit device (DEKA) is
    # selected canonically instead of creating a redundant device question.
    assert updated.constraints.device_key == DEKA
    assert updated.service_device_keys == {UNDERARM: DEKA, FULL_ARMS: DEKA}
    assert updated.derived.availability_snapshot_id is None
    assert updated.derived.selected_slot_ref is None
    assert updated.proposal is None



def test_unconfigured_device_variant_is_not_a_service_compatibility_choice() -> None:
    catalog = _catalog(arms_devices=[CANDELA, DEKA])
    arms = next(row for row in catalog["services"] if row["id"] == FULL_ARMS)
    arms["laser_devices"][0]["configured"] = False
    context = build_semantic_context(catalog)
    arms_ref = next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "service" and target.canonical_id == FULL_ARMS
    )
    compatible_refs = context.model_input["service_device_refs"][arms_ref]
    compatible_keys = [context.resolve(ref, expected_kind="device") for ref in compatible_refs]
    assert compatible_keys == [DEKA]
