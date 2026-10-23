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
DOCTOR_2 = "doctor-sarah"
F2_PREVIOUS_DATE = "2026-10-24"
F2_NEW_DATE = "2026-10-25"
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
                },
                {
                    "id": DOCTOR_2,
                    "name": "Sarah Adel",
                    "service_ids": [UNDERARM, FULL_ARMS, FACIAL],
                    "branch_ids": [BRANCH],
                },
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
    presented_date: str = TUESDAY,
) -> tuple[SemanticContext, dict[str, object]]:
    semantic = _base_semantic()
    raw: dict[str, object] = {
        "operation_type": "availability",
        "service_id": UNDERARM,
        "doctor_id": DOCTOR,
        "device_key": CANDELA,
        "date": {"mode": "exact", "start_date": presented_date, "end_date": None},
        "package_usage": "unspecified",
        "availability_reference_options": [
            {
                "option_ref": "opt_tuesday",
                "concrete": True,
                "start_local": f"{presented_date}T16:00:00+03:00",
                "end_local": f"{presented_date}T16:15:00+03:00",
                "start_time_24h": "16:00",
                "end_time_24h": "16:15",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
                "slot": {
                    "service_id": UNDERARM,
                    "doctor_id": DOCTOR,
                    "branch_id": BRANCH,
                    "laser_device_key": CANDELA,
                    "start_at": f"{presented_date}T16:00:00+03:00",
                    "end_at": f"{presented_date}T16:15:00+03:00",
                    "start_local": f"{presented_date}T16:00:00+03:00",
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
    date: str | None = WEDNESDAY,
    time: str | None = None,
    doctor_id: str | None = None,
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
    doctor = (
        EntityReference(ref=_ref(semantic, kind="doctor", canonical_id=doctor_id))
        if doctor_id is not None
        else None
    )
    return TurnOperation(
        type="availability",
        entities=TurnEntities(
            service=service,
            doctor=doctor,
            device=device,
            date=(DateConstraint(mode="exact", start_date=date) if date is not None else None),
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


def test_relative_time_followup_reuses_verified_scope_and_replaces_only_time() -> None:
    for constraint in (
        TimeConstraint(mode="nearest"),
        TimeConstraint(mode="before", start_time="18:00"),
        TimeConstraint(mode="after", start_time="18:00"),
    ):
        semantic, raw = _presented_context(old_time="18:00")
        model_input = dict(semantic.model_input)
        model_input["availability_followup_intent"] = {
            "action": "new_search",
            "time_constraint": constraint.model_dump(mode="json"),
        }
        semantic = SemanticContext(
            model_input=model_input,
            reference_map=semantic.reference_map,
            server_metadata=semantic.server_metadata,
        )
        turn = TiaTurnUnderstanding(
            operations=[
                TurnOperation(
                    type="availability",
                    entities=TurnEntities(),
                    execution_intent="informational",
                    continues_previous=True,
                )
            ]
        )

        merged = merge_presented_availability_context(turn, semantic).operations[0]
        step = plan_turn(
            TiaTurnUnderstanding(operations=[merged]),
            PlannerContext(
                semantic_context=semantic,
                active_task=None,
                now=NOW,
                availability_reference_context=raw,
            ),
        ).steps[0]

        assert semantic.resolve(merged.entities.service.ref, expected_kind="service") == UNDERARM
        assert semantic.resolve(merged.entities.doctor.ref, expected_kind="doctor") == DOCTOR
        assert semantic.resolve(merged.entities.device.ref, expected_kind="device") == CANDELA
        assert merged.entities.date == DateConstraint(mode="exact", start_date=TUESDAY)
        assert merged.entities.time == constraint
        assert step.disposition == "read"
        assert step.write_intent is None
        params = step.reads[0].parameters
        assert params["service_id"] == UNDERARM
        assert params["doctor_id"] == DOCTOR
        assert params["device_key"] == CANDELA
        assert params["date"]["start_date"] == TUESDAY
        assert params["time"]["mode"] == constraint.mode
        assert params["time"].get("start_time") == constraint.start_time


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
        _operation(semantic, service_id=FULL_ARMS, carry_old_device_ref=True),
    )
    assert semantic.resolve(operation.entities.service.ref, expected_kind="service") == FULL_ARMS
    assert semantic.resolve(operation.entities.device.ref, expected_kind="device") == CANDELA
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


def test_f2_a_explicit_date_and_time_replace_date_and_preserve_doctor() -> None:
    semantic, raw = _presented_context(presented_date=F2_PREVIOUS_DATE)
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, date=F2_NEW_DATE, time="16:00"),
    )
    assert operation.entities.date == DateConstraint(mode="exact", start_date=F2_NEW_DATE)
    assert operation.entities.time == TimeConstraint(
        mode="exact", start_time="16:00", start_time_ambiguity="none"
    )
    assert semantic.resolve(operation.entities.doctor.ref, expected_kind="doctor") == DOCTOR
    assert step.write_intent is None
    assert step.reads[0].kind == "availability"
    params = step.reads[0].parameters
    assert params["date"]["start_date"] == F2_NEW_DATE
    assert params["time"]["start_time"] == "16:00"
    assert params["doctor_id"] == DOCTOR


def test_f2_b_explicit_date_only_does_not_resurrect_stale_time() -> None:
    semantic, raw = _presented_context(old_time="11:00", presented_date=F2_PREVIOUS_DATE)
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, date=F2_NEW_DATE, time=None),
    )
    assert operation.entities.date == DateConstraint(mode="exact", start_date=F2_NEW_DATE)
    assert operation.entities.time is None
    assert step.write_intent is None
    params = step.reads[0].parameters
    assert params["date"]["start_date"] == F2_NEW_DATE
    assert "time" not in params


def test_f2_c_explicit_time_only_inherits_verified_date_and_doctor() -> None:
    semantic, raw = _presented_context(old_time="11:00", presented_date=F2_PREVIOUS_DATE)
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, date=None, time="16:00"),
    )
    assert operation.entities.date == DateConstraint(mode="exact", start_date=F2_PREVIOUS_DATE)
    assert operation.entities.time == TimeConstraint(
        mode="exact", start_time="16:00", start_time_ambiguity="none"
    )
    assert semantic.resolve(operation.entities.doctor.ref, expected_kind="doctor") == DOCTOR
    assert step.write_intent is None
    params = step.reads[0].parameters
    assert params["date"]["start_date"] == F2_PREVIOUS_DATE
    assert params["time"]["start_time"] == "16:00"
    assert params["doctor_id"] == DOCTOR


def test_f2_d_explicit_date_and_doctor_replace_both_dimensions() -> None:
    semantic, raw = _presented_context(presented_date=F2_PREVIOUS_DATE)
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, date=F2_NEW_DATE, doctor_id=DOCTOR_2),
    )
    assert operation.entities.date == DateConstraint(mode="exact", start_date=F2_NEW_DATE)
    assert semantic.resolve(operation.entities.doctor.ref, expected_kind="doctor") == DOCTOR_2
    assert step.write_intent is None
    params = step.reads[0].parameters
    assert params["date"]["start_date"] == F2_NEW_DATE
    assert params["doctor_id"] == DOCTOR_2


def test_f2_e_explicit_date_time_and_doctor_replace_all_dimensions() -> None:
    semantic, raw = _presented_context(old_time="11:00", presented_date=F2_PREVIOUS_DATE)
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(
            semantic,
            date=F2_NEW_DATE,
            time="16:00",
            doctor_id=DOCTOR_2,
        ),
    )
    assert operation.entities.date == DateConstraint(mode="exact", start_date=F2_NEW_DATE)
    assert operation.entities.time == TimeConstraint(
        mode="exact", start_time="16:00", start_time_ambiguity="none"
    )
    assert semantic.resolve(operation.entities.doctor.ref, expected_kind="doctor") == DOCTOR_2
    assert step.write_intent is None
    params = step.reads[0].parameters
    assert params["date"]["start_date"] == F2_NEW_DATE
    assert params["time"]["start_time"] == "16:00"
    assert params["doctor_id"] == DOCTOR_2


def test_f2_f_omitted_date_keeps_verified_date_authoritative() -> None:
    semantic, raw = _presented_context(presented_date=F2_PREVIOUS_DATE)
    operation, step = _merge_and_plan(
        semantic,
        raw,
        _operation(semantic, date=None, time="16:00"),
    )
    assert operation.entities.date == DateConstraint(mode="exact", start_date=F2_PREVIOUS_DATE)
    assert operation.entities.time == TimeConstraint(
        mode="exact", start_time="16:00", start_time_ambiguity="none"
    )
    assert semantic.resolve(operation.entities.doctor.ref, expected_kind="doctor") == DOCTOR
    assert step.write_intent is None
    params = step.reads[0].parameters
    assert params["date"]["start_date"] == F2_PREVIOUS_DATE
    assert params["time"]["start_time"] == "16:00"
    assert params["doctor_id"] == DOCTOR
