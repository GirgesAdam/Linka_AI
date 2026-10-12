from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_availability_reference_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_presented_availability_context
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.reference_resolution import resolve_verified_availability_reference

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
TUESDAY = "2026-10-20"
WEDNESDAY = "2026-10-21"
UNDERARM = "svc-underarm"
FULL_ARMS = "svc-full-arms"
FACIAL = "svc-facial"
DOCTOR = "doctor-youssef"
BRANCH = "branch-new-cairo"
CANDELA = "candela"


def _base_semantic() -> SemanticContext:
    return build_semantic_context(
        {
            "services": [
                {
                    "id": UNDERARM,
                    "name": "Underarm Laser",
                    "requires_laser_device": True,
                    "laser_devices": [{"device_key": CANDELA, "device_name": "Candela"}],
                },
                {
                    "id": FULL_ARMS,
                    "name": "Full Arms Laser",
                    "requires_laser_device": True,
                    "laser_devices": [{"device_key": CANDELA, "device_name": "Candela"}],
                },
                {
                    "id": FACIAL,
                    "name": "Deep Facial Cleansing",
                    "requires_laser_device": False,
                },
            ],
            "branches": [{"id": BRANCH, "name": "New Cairo", "city": "Cairo"}],
            "doctors": [
                {
                    "id": DOCTOR,
                    "name": "Youssef Samir",
                    "service_ids": [UNDERARM, FULL_ARMS, FACIAL],
                    "branch_ids": [BRANCH],
                }
            ],
            "laser_devices": [
                {"device_key": CANDELA, "device_name": "Candela"},
            ],
            "appointments": [],
            "packages": [],
        }
    )


def _ref(context: SemanticContext, *, kind: str, canonical_id: str) -> str:
    for ref, target in context.reference_map.items():
        if target.kind == kind and target.canonical_id == canonical_id:
            return ref
    raise AssertionError(f"missing {kind} ref for {canonical_id}")


def _presented_context(
    *,
    recent_read_operation: str | None = None,
    old_time: str | None = None,
) -> tuple[SemanticContext, dict[str, object]]:
    semantic = _base_semantic()
    raw: dict[str, object] = {
        "operation_type": "availability",
        "service_id": UNDERARM,
        "doctor_id": DOCTOR,
        "device_key": CANDELA,
        "date": {"mode": "exact", "start_date": TUESDAY, "end_date": None},
        "package_usage": "unspecified",
        "availability_reference_options": [
            {
                "option_ref": "opt_tuesday",
                "concrete": True,
                "start_local": f"{TUESDAY}T16:00:00+03:00",
                "end_local": f"{TUESDAY}T16:15:00+03:00",
                "start_time_24h": "16:00",
                "end_time_24h": "16:15",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
                "slot": {
                    "service_id": UNDERARM,
                    "doctor_id": DOCTOR,
                    "branch_id": BRANCH,
                    "laser_device_key": CANDELA,
                    "start_at": f"{TUESDAY}T16:00:00+03:00",
                    "end_at": f"{TUESDAY}T16:15:00+03:00",
                    "start_local": f"{TUESDAY}T16:00:00+03:00",
                    "start_time_24h": "16:00",
                },
            }
        ],
    }
    if old_time is not None:
        raw["time"] = {
            "mode": "exact",
            "start_time": old_time,
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        }
    semantic = with_safe_availability_reference_context(
        semantic,
        availability_context=raw,
    )
    model_input = dict(semantic.model_input)
    model_input["availability_followup_intent"] = {"action": "new_search"}
    if recent_read_operation is not None:
        model_input["recent_verified_read"] = {"operation_type": recent_read_operation}
    semantic = SemanticContext(
        model_input=model_input,
        reference_map=semantic.reference_map,
        server_metadata=semantic.server_metadata,
    )
    return semantic, raw


def _operation(
    semantic: SemanticContext,
    *,
    service_id: str | None = None,
    empty_service: bool = False,
    carry_old_device_ref: bool = False,
    time: str | None = None,
) -> TurnOperation:
    service = None
    if empty_service:
        service = EntityReference()
    elif service_id is not None:
        service = EntityReference(ref=_ref(semantic, kind="service", canonical_id=service_id))
    device = (
        EntityReference(ref=_ref(semantic, kind="device", canonical_id=CANDELA))
        if carry_old_device_ref
        else None
    )
    return TurnOperation(
        type="availability",
        entities=TurnEntities(
            service=service,
            device=device,
            date=DateConstraint(mode="exact", start_date=WEDNESDAY),
            time=(
                TimeConstraint(mode="exact", start_time=time, start_time_ambiguity="none")
                if time is not None
                else None
            ),
        ),
        execution_intent="informational",
        continues_previous=True,
    )


def _merge_and_plan(
    semantic: SemanticContext,
    raw: dict[str, object],
    operation: TurnOperation,
):
    merged = merge_presented_availability_context(
        TiaTurnUnderstanding(operations=[operation]),
        semantic,
    )
    step = plan_turn(
        merged,
        PlannerContext(
            semantic_context=semantic,
            active_task=None,
            now=NOW,
            availability_reference_context=raw,
        ),
    ).steps[0]
    return merged.operations[0], step


def test_d1_original_ra6_rebinds_server_owned_scope_after_price_side_read() -> None:
    semantic, raw = _presented_context(recent_read_operation="pricing")
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, empty_service=True, carry_old_device_ref=True),
    )
    assert semantic.resolve(operation.entities.service.ref, expected_kind="service") == UNDERARM
    assert semantic.resolve(operation.entities.device.ref, expected_kind="device") == CANDELA
    assert step.disposition == "read"
    assert step.clarification_field is None
    assert step.response_goal == "present_availability"
    assert step.reads[0].kind == "availability"
    assert step.reads[0].parameters["service_id"] == UNDERARM
    assert step.reads[0].parameters["device_key"] == CANDELA
    assert step.reads[0].parameters["date"]["start_date"] == WEDNESDAY


def test_d2_direct_date_replacement_reuses_presented_scope() -> None:
    semantic, raw = _presented_context()
    operation, step = _merge_and_plan(semantic, raw, _operation(semantic))
    assert semantic.resolve(operation.entities.service.ref, expected_kind="service") == UNDERARM
    assert semantic.resolve(operation.entities.device.ref, expected_kind="device") == CANDELA
    assert step.disposition == "read"
    assert step.reads[0].parameters["date"]["start_date"] == WEDNESDAY


def test_d3_multiple_side_reads_do_not_change_authoritative_presented_scope() -> None:
    for recent in ("pricing", "doctor_info", "clinic_info"):
        semantic, raw = _presented_context(recent_read_operation=recent)
        operation, step = _merge_and_plan(semantic, raw, _operation(semantic, empty_service=True))
        assert semantic.resolve(operation.entities.service.ref, expected_kind="service") == UNDERARM
        assert semantic.resolve(operation.entities.device.ref, expected_kind="device") == CANDELA
        assert step.disposition == "read"
        assert step.reads[0].parameters["date"]["start_date"] == WEDNESDAY


def test_d4_explicit_service_and_date_change_does_not_retain_old_service() -> None:
    semantic, raw = _presented_context(recent_read_operation="pricing")
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, service_id=FULL_ARMS),
    )
    assert semantic.resolve(operation.entities.service.ref, expected_kind="service") == FULL_ARMS
    assert step.disposition == "read"
    assert step.reads[0].parameters["service_id"] == FULL_ARMS
    assert step.reads[0].parameters["date"]["start_date"] == WEDNESDAY


def test_d5_compatible_presented_device_survives_date_only_change() -> None:
    semantic, raw = _presented_context(recent_read_operation="pricing")
    operation, step = _merge_and_plan(semantic, raw, _operation(semantic))
    assert semantic.resolve(operation.entities.device.ref, expected_kind="device") == CANDELA
    assert step.reads[0].parameters["device_key"] == CANDELA


def test_d6_service_change_drops_bare_old_device_authority() -> None:
    semantic, raw = _presented_context(recent_read_operation="pricing")
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, service_id=FACIAL, carry_old_device_ref=True),
    )
    assert semantic.resolve(operation.entities.service.ref, expected_kind="service") == FACIAL
    assert operation.entities.device is None
    assert step.disposition == "read"
    assert step.reads[0].parameters["service_id"] == FACIAL
    assert "device_key" not in step.reads[0].parameters


def test_d7_old_tuesday_ref_cannot_resolve_to_tuesday_after_scope_replacement() -> None:
    semantic, _raw = _presented_context()
    fresh = {
        "availability_reference_options": [
            {
                "option_ref": "opt_tuesday",
                "index": 1,
                "concrete": True,
                "slot": {
                    "service_id": UNDERARM,
                    "doctor_id": DOCTOR,
                    "branch_id": BRANCH,
                    "laser_device_key": CANDELA,
                    "start_at": f"{WEDNESDAY}T16:30:00+03:00",
                    "start_local": f"{WEDNESDAY}T16:30:00+03:00",
                },
            }
        ]
    }
    resolved = resolve_verified_availability_reference(
        Selection(kind="ref", ref="opt_tuesday"),
        fresh,
    )
    assert resolved["status"] == "resolved"
    assert resolved["option"]["slot"]["start_local"].startswith(WEDNESDAY)
    assert not resolved["option"]["slot"]["start_local"].startswith(TUESDAY)
    assert semantic is not None


def test_d8_date_replacement_never_resurrects_older_presented_time() -> None:
    semantic, raw = _presented_context(old_time="11:00")
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, time="12:00"),
    )
    assert operation.entities.time is not None
    assert operation.entities.time.start_time == "12:00"
    assert step.reads[0].parameters["time"]["start_time"] == "12:00"
