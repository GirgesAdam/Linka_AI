from datetime import UTC, datetime, time

from app.agents.availability_presentation import availability_windows_from_slots
from app.agents.v2.availability_composer import render_embedded_requested_time_unavailable
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_availability_reference_context
from app.agents.v2.turn_contract import (
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_presented_availability_context
from app.services.agent_v2.active_task_progress import plan_active_task_progress
from app.services.agent_v2.multi_service_booking import common_laser_device_keys
from app.services.agent_v2.orchestrator import _auto_resolve_single_booking_device
from app.services.agent_v2.outcome_builder import _service_catalog_facts
from app.services.agent_v2.planner import PlannerContext, PlanStep, plan_turn
from app.services.agent_v2.read_executor import (
    ReadExecutionBundle,
    ReadResult,
    _nearest_alternative_payloads,
)
from app.services.agent_v2.reference_resolution import build_availability_reference_options
from app.services.agent_v2.response_contract import _service_commercial_truth
from app.services.agent_v2.state import BookingTaskState, CustomerConstraints, WriteAuthorization
from app.services.agent_v2.state_executor import reconcile_booking_service_set_state
from app.services.agent_v2.state_rules import preserve_task_for_side_read

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
DATE = "2026-10-20"
UNDERARM = "svc-underarm"
FULL_ARMS = "svc-full-arms"
DOCTOR = "doctor-youssef"
BRANCH = "branch-cairo"
CANDELA = "candela"
DEKA = "deka"
SOPRANO = "soprano"


def _device(key: str, *, price: int = 55_000, duration: int = 15) -> dict[str, object]:
    return {
        "device_key": key,
        "device_name": key.title(),
        "configured": True,
        "price_minor": price,
        "currency": "EGP",
        "duration_minutes": duration,
    }


def _catalog(
    *,
    underarm_devices: list[dict[str, object]],
    arms_devices: list[dict[str, object]] | None = None,
    clinic_devices: list[str] | None = None,
) -> dict[str, object]:
    arms = arms_devices if arms_devices is not None else list(underarm_devices)
    keys = clinic_devices or list(dict.fromkeys(
        str(row["device_key"]) for row in [*underarm_devices, *arms]
    ))
    return {
        "services": [
            {
                "id": UNDERARM,
                "name": "Underarm Laser",
                "duration_minutes": 15,
                "requires_laser_device": True,
                "laser_devices": underarm_devices,
            },
            {
                "id": FULL_ARMS,
                "name": "Full Arms Laser",
                "duration_minutes": 30,
                "requires_laser_device": True,
                "laser_devices": arms,
            },
        ],
        "laser_devices": [
            {"device_key": key, "device_name": key.title()} for key in keys
        ],
        "branches": [{"id": BRANCH, "name": "Cairo"}],
        "doctors": [
            {
                "id": DOCTOR,
                "name": "Youssef Samir",
                "service_ids": [UNDERARM, FULL_ARMS],
                "branch_ids": [BRANCH],
            }
        ],
        "appointments": [],
        "packages": [],
    }


def _task(services: list[str], *, main: str, device: str | None = None) -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id=main,
            branch_id=BRANCH,
            device_key=device,
        ),
        requested_service_ids=list(services),
        service_device_keys=(
            {service_id: device for service_id in services}
            if device is not None
            else {}
        ),
    )


def _slot(hhmm: str) -> dict[str, object]:
    hour, minute = (int(part) for part in hhmm.split(":"))
    end_minute = minute + 15
    end_hour = hour + end_minute // 60
    end_minute %= 60
    return {
        "service_id": UNDERARM,
        "service_name": "Underarm Laser",
        "doctor_id": DOCTOR,
        "doctor_name": "Youssef Samir",
        "laser_device_key": CANDELA,
        "laser_device_name": "Candela",
        "start_at": f"{DATE}T{hhmm}:00+03:00",
        "end_at": f"{DATE}T{end_hour:02d}:{end_minute:02d}:00+03:00",
        "start_local": f"{DATE}T{hhmm}:00+03:00",
        "end_local": f"{DATE}T{end_hour:02d}:{end_minute:02d}:00+03:00",
        "start_time_24h": hhmm,
        "end_time_24h": f"{end_hour:02d}:{end_minute:02d}",
        "duration_minutes": 15,
        "price_minor": 55_000,
        "currency": "EGP",
    }


def _presented_semantic() -> tuple[SemanticContext, dict[str, object]]:
    semantic = build_semantic_context(_catalog(underarm_devices=[_device(CANDELA)]))
    service_ref = next(
        ref for ref, target in semantic.reference_map.items()
        if target.kind == "service" and target.canonical_id == UNDERARM
    )
    doctor_ref = next(
        ref for ref, target in semantic.reference_map.items()
        if target.kind == "doctor" and target.canonical_id == DOCTOR
    )
    device_ref = next(
        ref for ref, target in semantic.reference_map.items()
        if target.kind == "device" and target.canonical_id == CANDELA
    )
    raw: dict[str, object] = {
        "operation_type": "availability",
        "service_id": UNDERARM,
        "service_ref": service_ref,
        "doctor_id": DOCTOR,
        "doctor_ref": doctor_ref,
        "device_key": CANDELA,
        "device_ref": device_ref,
        "date": {"mode": "exact", "start_date": DATE, "end_date": None},
        "package_usage": "unspecified",
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": True,
                "start_local": f"{DATE}T16:00:00+03:00",
                "end_local": f"{DATE}T16:15:00+03:00",
                "start_time_24h": "16:00",
                "end_time_24h": "16:15",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
                "slot": {
                    "service_id": UNDERARM,
                    "doctor_id": DOCTOR,
                    "branch_id": BRANCH,
                    "laser_device_key": CANDELA,
                    "start_at": f"{DATE}T16:00:00+03:00",
                    "start_local": f"{DATE}T16:00:00+03:00",
                },
            }
        ],
    }
    semantic = with_safe_availability_reference_context(
        semantic,
        availability_context=raw,
    )
    model_input = dict(semantic.model_input)
    model_input["availability_followup_intent"] = {"action": "new_search"}
    return SemanticContext(
        model_input=model_input,
        reference_map=semantic.reference_map,
        server_metadata=semantic.server_metadata,
    ), raw


def _service_read(devices: list[dict[str, object]]) -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[
            ReadResult(
                kind="service_catalog",
                ok=True,
                payload={
                    "service": {
                        "id": UNDERARM,
                        "name": "Underarm Laser",
                        "requires_laser_device": True,
                        "laser_devices": devices,
                    }
                },
            )
        ]
    )


def _device_step(common: list[str]) -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="read",
        state_action="update_active",
        response_goal="answer_price",
        facts={
            "service_id": UNDERARM,
            "booking_device_price_step": True,
            "compatible_visit_device_keys": common,
        },
    )


def test_a1_ra7_canonical_hole_produces_truthful_split_windows_and_refs() -> None:
    raw = [_slot("16:00"), _slot("16:30"), _slot("16:45"), _slot("17:00")]
    assert [row["start_time_24h"] for row in raw] == ["16:00", "16:30", "16:45", "17:00"]
    assert all(row["start_time_24h"] != "16:15" for row in raw)
    windows = availability_windows_from_slots(raw)
    assert [(row["start_time_24h"], row["end_time_24h"]) for row in windows] == [
        ("16:00", "16:00"),
        ("16:30", "17:00"),
    ]
    refs = build_availability_reference_options(displayed_windows=windows, verified_slots=raw)
    assert refs[0]["concrete"] is True
    assert refs[0]["slot"]["start_time_24h"] == "16:00"
    assert refs[1]["concrete"] is False
    assert refs[1]["canonical_slot_count"] == 3


def test_a2_compression_never_bridges_unavailable_1615_gap() -> None:
    windows = availability_windows_from_slots([_slot("16:00"), _slot("16:30"), _slot("16:45")])
    assert len(windows) == 2
    assert windows[0]["start_time_24h"] == windows[0]["end_time_24h"] == "16:00"
    assert windows[1]["start_time_24h"] == "16:30"


def test_a3_exact_time_new_search_rebinds_verified_date_doctor_device_and_offers_verified_alternatives() -> None:
    semantic, raw_context = _presented_semantic()
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="availability",
                entities=TurnEntities(
                    time=TimeConstraint(mode="exact", start_time="16:15", start_time_ambiguity="none")
                ),
                execution_intent="informational",
                continues_previous=True,
            )
        ]
    )
    merged = merge_presented_availability_context(turn, semantic)
    operation = merged.operations[0]
    assert operation.entities.date is not None
    assert operation.entities.date.start_date == DATE
    assert semantic.resolve(operation.entities.service.ref, expected_kind="service") == UNDERARM
    assert semantic.resolve(operation.entities.doctor.ref, expected_kind="doctor") == DOCTOR
    assert semantic.resolve(operation.entities.device.ref, expected_kind="device") == CANDELA
    step = plan_turn(
        merged,
        PlannerContext(
            semantic_context=semantic,
            active_task=None,
            now=NOW,
            availability_reference_context=raw_context,
        ),
    ).steps[0]
    assert step.reads[0].parameters["date"]["start_date"] == DATE
    assert step.reads[0].parameters["doctor_id"] == DOCTOR
    assert step.reads[0].parameters["device_key"] == CANDELA
    nearest = _nearest_alternative_payloads(
        [_slot("16:00"), _slot("16:30"), _slot("16:45")],
        anchor=time(16, 15),
    )
    assert [row["start_time_24h"] for row in nearest] == ["16:00", "16:30"]
    text = render_embedded_requested_time_unavailable(
        {
            "requested_time": "16:15",
            "nearest_alternative_windows": availability_windows_from_slots(nearest),
        },
        arabic=True,
    )
    assert "مفيش وقت تاني متاح" not in text
    assert "4 مساءً" in text
    assert "4:30 مساءً" in text


def test_a4_no_alternatives_is_the_only_case_that_offers_another_day() -> None:
    text = render_embedded_requested_time_unavailable(
        {"requested_time": "16:15", "nearest_alternative_windows": []},
        arabic=True,
    )
    assert "مفيش وقت تاني متاح هنا" in text
    assert "أقرب يوم" in text


def test_d1_one_compatible_device_is_canonical_without_device_choice() -> None:
    catalog = _catalog(underarm_devices=[_device(CANDELA)])
    state = reconcile_booking_service_set_state(_task([UNDERARM], main=UNDERARM), catalog=catalog)
    assert state.constraints.device_key == CANDELA
    assert state.service_device_keys == {UNDERARM: CANDELA}
    step = plan_active_task_progress(state, operation_index=0, context=build_semantic_context(catalog))
    assert step.clarification_field != "device"


def test_d2_two_compatible_devices_are_not_auto_selected() -> None:
    devices = [_device(CANDELA, price=55_000), _device(DEKA, price=50_000)]
    catalog = _catalog(underarm_devices=devices)
    state = reconcile_booking_service_set_state(_task([UNDERARM], main=UNDERARM), catalog=catalog)
    assert state.constraints.device_key is None
    step = plan_active_task_progress(state, operation_index=0, context=build_semantic_context(catalog))
    assert step.facts["compatible_visit_device_keys"] == [CANDELA, DEKA]
    resolved = _auto_resolve_single_booking_device(step, _service_read(devices))
    assert resolved.facts.get("device_key") is None
    facts = _service_catalog_facts(_service_read(devices).results[0], {"price"})
    truth = _service_commercial_truth({"service_catalog": facts})
    assert truth is not None
    assert truth.kind == "service_device_price_options"
    assert truth.complete_set is True
    assert [(option.device_name, option.amount) for option in truth.options] == [
        ("Candela", "550.00"),
        ("Deka", "500.00"),
    ]


def test_d3_three_compatible_devices_all_remain_customer_choices() -> None:
    devices = [_device(CANDELA), _device(DEKA), _device(SOPRANO)]
    catalog = _catalog(underarm_devices=devices)
    state = reconcile_booking_service_set_state(_task([UNDERARM], main=UNDERARM), catalog=catalog)
    step = plan_active_task_progress(state, operation_index=0, context=build_semantic_context(catalog))
    assert step.facts["compatible_visit_device_keys"] == [CANDELA, DEKA, SOPRANO]
    assert _auto_resolve_single_booking_device(step, _service_read(devices)).facts.get("device_key") is None
    facts = _service_catalog_facts(_service_read(devices).results[0], {"price"})
    truth = _service_commercial_truth({"service_catalog": facts})
    assert truth is not None
    assert truth.complete_set is True
    assert [option.device_name for option in truth.options] == ["Candela", "Deka", "Soprano"]


def test_d4_two_devices_in_clinic_but_one_service_compatible_auto_selects_service_device() -> None:
    catalog = _catalog(
        underarm_devices=[_device(CANDELA)],
        clinic_devices=[CANDELA, DEKA],
    )
    state = reconcile_booking_service_set_state(_task([UNDERARM], main=UNDERARM), catalog=catalog)
    assert state.constraints.device_key == CANDELA


def test_d5_multi_service_intersection_one_auto_selects_visit_level_device() -> None:
    catalog = _catalog(
        underarm_devices=[_device(CANDELA), _device(DEKA)],
        arms_devices=[_device(CANDELA)],
    )
    assert common_laser_device_keys(catalog, [UNDERARM, FULL_ARMS]) == [CANDELA]
    state = reconcile_booking_service_set_state(
        _task([UNDERARM, FULL_ARMS], main=FULL_ARMS),
        catalog=catalog,
    )
    assert state.constraints.device_key == CANDELA
    assert state.service_device_keys == {UNDERARM: CANDELA, FULL_ARMS: CANDELA}


def test_d6_multi_service_intersection_two_requires_one_visit_level_choice() -> None:
    devices = [_device(CANDELA), _device(DEKA)]
    catalog = _catalog(underarm_devices=devices, arms_devices=devices)
    state = reconcile_booking_service_set_state(
        _task([UNDERARM, FULL_ARMS], main=FULL_ARMS), catalog=catalog
    )
    assert state.constraints.device_key is None
    step = plan_active_task_progress(state, operation_index=0, context=build_semantic_context(catalog))
    assert step.facts["compatible_visit_device_keys"] == [CANDELA, DEKA]
    assert len(step.reads) == 1
    assert step.reads[0].kind == "service_catalog_set"
    assert step.reads[0].parameters["service_ids"] == [UNDERARM, FULL_ARMS]


def test_d7_selected_device_survives_side_reads_without_reasking_device() -> None:
    state = _task([UNDERARM], main=UNDERARM, device=DEKA)
    for _ in range(3):
        state = preserve_task_for_side_read(state)
    assert state.constraints.device_key == DEKA
    assert state.service_device_keys == {UNDERARM: DEKA}


def test_d8_service_correction_replaces_stale_device_with_only_common_device() -> None:
    catalog = _catalog(
        underarm_devices=[_device(CANDELA), _device(DEKA)],
        arms_devices=[_device(CANDELA)],
    )
    corrected = _task([FULL_ARMS], main=FULL_ARMS, device=DEKA)
    updated = reconcile_booking_service_set_state(corrected, catalog=catalog)
    assert updated.constraints.device_key == CANDELA
    assert updated.service_device_keys == {FULL_ARMS: CANDELA}
    assert updated.proposal is None
    assert updated.option_snapshot is None


def test_d9_device_specific_prices_remain_complete_and_distinct() -> None:
    result = ReadResult(
        kind="service_catalog",
        ok=True,
        payload={
            "service": {
                "name": "Underarm Laser",
                "requires_laser_device": True,
                "laser_devices": [
                    _device(CANDELA, price=55_000),
                    _device(DEKA, price=50_000),
                ],
            }
        },
    )
    facts = _service_catalog_facts(result, {"price"})["service"]
    assert [(row["device_name"], row["price"]) for row in facts["laser_devices"]] == [
        ("Candela", "550.00 EGP"),
        ("Deka", "500.00 EGP"),
    ]


def test_d10_device_specific_durations_are_exposed_before_availability() -> None:
    devices = [
        _device(CANDELA, price=55_000, duration=15),
        _device(DEKA, price=50_000, duration=30),
    ]
    result = ReadResult(
        kind="service_catalog",
        ok=True,
        payload={
            "service": {
                "name": "Underarm Laser",
                "requires_laser_device": True,
                "laser_devices": devices,
            }
        },
    )
    wrapped = _service_catalog_facts(result, {"price"})
    facts = wrapped["service"]
    assert [row["duration_minutes"] for row in facts["laser_devices"]] == [15, 30]
    truth = _service_commercial_truth({"service_catalog": wrapped})
    assert truth is not None
    assert [option.duration_minutes for option in truth.options] == [15, 30]
    catalog = _catalog(underarm_devices=devices)
    state = reconcile_booking_service_set_state(_task([UNDERARM], main=UNDERARM), catalog=catalog)
    step = plan_active_task_progress(state, operation_index=0, context=build_semantic_context(catalog))
    assert [read.kind for read in step.reads] == ["service_catalog"]
    assert all(read.kind != "availability" for read in step.reads)


def test_d11_zero_common_devices_fails_closed_before_availability_or_write() -> None:
    catalog = _catalog(
        underarm_devices=[_device(DEKA)],
        arms_devices=[_device(CANDELA)],
    )
    state = reconcile_booking_service_set_state(
        _task([UNDERARM, FULL_ARMS], main=FULL_ARMS), catalog=catalog
    )
    assert state.constraints.device_key is None
    step = plan_active_task_progress(state, operation_index=0, context=build_semantic_context(catalog))
    assert step.disposition == "clarify"
    assert step.facts["multi_service_no_common_device"] is True
    assert step.reads == []
    assert step.write_intent is None


def test_singleton_auto_resolver_never_selects_outside_common_visit_set() -> None:
    step = _device_step([CANDELA])
    resolved = _auto_resolve_single_booking_device(
        step,
        _service_read([_device(CANDELA), _device(DEKA)]),
    )
    assert resolved.facts["device_key"] == CANDELA
