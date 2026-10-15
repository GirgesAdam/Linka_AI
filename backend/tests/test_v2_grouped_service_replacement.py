from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.grouped_booking_continuity import explicitly_narrow_grouped_booking_task
from app.services.agent_v2.orchestrator import _prepare_booking_proposal
from app.services.agent_v2.planner import PlanStep, WriteIntent
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingProposal,
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.turn_normalization import collapse_booking_service_sets

A = "svc-underarm"
B = "svc-bikini"
C = "svc-full-arms"
D = "svc-device-two-only"
BRANCH = "branch-1"
DOCTOR = "doctor-1"
DATE = DateConstraint(mode="exact", start_date="2026-10-17")
TIME = TimeConstraint(mode="exact", start_time="16:00")
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _device(key: str, duration: int) -> dict[str, object]:
    return {
        "device_key": key,
        "device_name": key,
        "price_minor": 50_000,
        "duration_minutes": duration,
        "configured": True,
    }


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {"id": A, "name": "Underarm", "duration_minutes": 20, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("v1", 20), _device("v2", 20)]},
            {"id": B, "name": "Bikini", "duration_minutes": 30, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("v1", 30)]},
            {"id": C, "name": "Full Arms", "duration_minutes": 40, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("v1", 40), _device("v2", 40)]},
            {"id": D, "name": "Device Two Only", "duration_minutes": 50, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("v2", 50)]},
        ],
        "doctors": [{"id": DOCTOR, "name": "Dr Test", "service_ids": [A, B, C, D], "branch_ids": [BRANCH]}],
        "branches": [{"id": BRANCH, "name": "Main"}],
        "appointments": [],
    }


def _refs(context) -> dict[str, str]:
    return {
        str(target.canonical_id): ref
        for ref, target in context.reference_map.items()
        if target.kind == "service"
    }


def _understanding(context, target: list[str], *, action: str) -> TiaTurnUnderstanding:
    refs = _refs(context)
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent="execute",
                active_task_relationship="continue",
                grouped_booking_action=action,
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[item] for item in target],
                        candidate_mode="set",
                    )
                ),
            )
        ]
    )


def _proposal(service_ids: list[str], *, main: str) -> BookingProposal:
    return BookingProposal(
        proposal_id="proposal-old",
        patient_id="patient-1",
        service_ids=service_ids,
        main_service_id=main,
        additional_service_ids=[item for item in service_ids if item != main],
        branch_id=BRANCH,
        doctor_id=DOCTOR,
        start_at=NOW + timedelta(days=4),
        end_at=NOW + timedelta(days=4, minutes=50),
        device_key="v1",
        additional_service_device_keys={item: "v1" for item in service_ids if item != main},
        task_version=3,
        proposal_version=1,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def _snapshot() -> OptionSnapshot:
    return OptionSnapshot(
        snapshot_id="slot-old",
        purpose="booking_slot",
        task_version=3,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        options=[OptionChoice(ref="slot-1", payload={"service_id": B, "device_key": "v1"})],
    )


def _state(
    requested: list[str],
    *,
    main: str | None = None,
    device: str | None = "v1",
    with_stale_authority: bool = False,
) -> BookingTaskState:
    main = main or max(requested, key={A: 20, B: 30, C: 40, D: 50}.get)
    return BookingTaskState(
        status="awaiting_choice" if with_stale_authority else "collecting",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(
            service_id=main,
            doctor_id=DOCTOR,
            branch_id=BRANCH,
            device_key=device,
            date=DATE,
            time=TIME,
        ),
        requested_service_ids=requested,
        service_device_keys={item: device for item in requested if device is not None},
        proposal=_proposal(requested, main=main) if with_stale_authority else None,
        derived=(
            DerivedBookingState(
                availability_snapshot_id="slot-old",
                selected_slot_ref="slot-1",
                device_compatible=True,
                doctor_compatible=True,
            )
            if with_stale_authority
            else DerivedBookingState()
        ),
        option_snapshot=_snapshot() if with_stale_authority else None,
        version=3,
    )


def _apply(state: BookingTaskState, target: list[str], *, action: str = "remove_other_components"):
    catalog = _catalog()
    context = build_semantic_context(catalog)
    collapsed, service_sets = collapse_booking_service_sets(
        _understanding(context, target, action=action),
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    narrowed = explicitly_narrow_grouped_booking_task(
        collapsed,
        active_task=state,
        context=context,
        catalog=catalog,
        booking_service_sets=service_sets,
    )
    return narrowed, service_sets


def test_sr1_replacement_is_authoritative() -> None:
    narrowed, service_sets = _apply(_state([A, B], main=B), [A, C])
    assert service_sets == {0: [A, C]}
    assert narrowed is not None
    assert narrowed.requested_service_ids == [A, C]
    assert B not in narrowed.requested_service_ids


def test_sr2_pure_narrowing_keeps_only_requested_component() -> None:
    narrowed, _ = _apply(_state([A, B], main=B), [A])
    assert narrowed is not None
    assert narrowed.requested_service_ids == [A]


def test_sr3_pure_addition_preserves_existing_set() -> None:
    state = _state([A, B], main=B)
    catalog = _catalog()
    context = build_semantic_context(catalog)
    _collapsed, service_sets = collapse_booking_service_sets(
        _understanding(context, [C], action="preserve_group"),
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    assert service_sets == {0: [A, B, C]}


def test_sr3_continue_active_informational_shape_preserves_pure_addition() -> None:
    state = _state([A, B], main=B)
    catalog = _catalog()
    context = build_semantic_context(catalog)
    refs = _refs(context)
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                execution_intent="informational",
                active_task_relationship="continue",
                grouped_booking_action="preserve_group",
                entities=TurnEntities(
                    service=EntityReference(
                        candidate_refs=[refs[A], refs[B], refs[C]],
                        candidate_mode="set",
                    )
                ),
            )
        ]
    )
    _collapsed, service_sets = collapse_booking_service_sets(
        turn,
        semantic_context=context,
        catalog=catalog,
        active_task=state,
    )
    assert service_sets == {0: [A, B, C]}


def test_sr4_replacement_with_multiple_retained_is_exact() -> None:
    narrowed, _ = _apply(_state([A, B, C], main=C), [A, C, D])
    assert narrowed is not None
    assert narrowed.requested_service_ids == [A, C, D]
    assert B not in narrowed.requested_service_ids


def test_sr5_full_replacement_accepts_only_new_service() -> None:
    narrowed, _ = _apply(_state([A, B], main=B), [C])
    assert narrowed is not None
    assert narrowed.requested_service_ids == [C]


def test_sr6_removed_main_is_recomputed_from_final_set() -> None:
    narrowed, _ = _apply(_state([B, C], main=C), [A, B])
    assert narrowed is not None
    assert narrowed.constraints.service_id == B


def test_sr7_compatible_visit_device_is_retained() -> None:
    narrowed, _ = _apply(_state([A, B], main=B, device="v1"), [A, C])
    assert narrowed is not None
    assert narrowed.constraints.device_key == "v1"
    assert narrowed.service_device_keys == {A: "v1", C: "v1"}


def test_sr8_incompatible_device_is_re_resolved_to_unique_common_device() -> None:
    narrowed, _ = _apply(_state([A, B], main=B, device="v1"), [A, D])
    assert narrowed is not None
    assert narrowed.constraints.device_key == "v2"
    assert narrowed.service_device_keys == {A: "v2", D: "v2"}


def test_sr9_no_common_device_fails_closed_at_task_authority_boundary() -> None:
    narrowed, _ = _apply(_state([A, B], main=B, device="v1", with_stale_authority=True), [B, D])
    assert narrowed is not None
    assert narrowed.constraints.device_key is None
    assert narrowed.service_device_keys == {}
    assert narrowed.proposal is None
    assert narrowed.option_snapshot is None
    assert narrowed.derived.selected_slot_ref is None


def test_sr10_service_mutation_clears_stale_proposal() -> None:
    narrowed, _ = _apply(_state([A, B], main=B, with_stale_authority=True), [A, C])
    assert narrowed is not None
    assert narrowed.proposal is None


def test_sr11_service_mutation_clears_stale_option_snapshot_and_slot_authority() -> None:
    narrowed, _ = _apply(_state([A, B], main=B, with_stale_authority=True), [A, C])
    assert narrowed is not None
    assert narrowed.option_snapshot is None
    assert narrowed.derived.availability_snapshot_id is None
    assert narrowed.derived.selected_slot_ref is None


def test_sr12_reapplying_same_corrected_complete_set_is_idempotent_for_services() -> None:
    first, _ = _apply(_state([A, B], main=B), [A, C])
    assert first is not None
    second, _ = _apply(first, [A, C])
    assert second is not None
    assert second.requested_service_ids == [A, C]
    assert len(second.requested_service_ids) == len(set(second.requested_service_ids))


def test_backend_authority_ignores_stale_old_main_and_uses_exact_target() -> None:
    narrowed, _ = _apply(_state([A, B], main=B), [A, C])
    assert narrowed is not None
    assert narrowed.requested_service_ids == [A, C]
    assert narrowed.constraints.service_id == C
    assert B not in narrowed.requested_service_ids



def test_replacement_proposal_boundary_uses_only_final_service_set(monkeypatch) -> None:
    narrowed, _ = _apply(_state([A, B], main=B), [A, C])
    assert narrowed is not None
    local_start = datetime(2026, 10, 17, 16, 0, tzinfo=ZoneInfo("Africa/Cairo"))
    start = local_start.astimezone(UTC)
    end = start + timedelta(minutes=40)
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="write_ready",
        write_intent=WriteIntent(
            kind="booking",
            authorized=True,
            parameters={
                "service_id": C,
                "doctor_id": DOCTOR,
                "branch_id": BRANCH,
                "device_key": "v1",
                "start_at": start.isoformat(),
                "package_usage": "unspecified",
            },
            requires_verification=True,
        ),
        response_goal="booking_completed",
        facts={
            "requested_service_ids": list(narrowed.requested_service_ids),
            "booking_proposal_required": True,
        },
    )
    reads = ReadExecutionBundle(
        results=[
            ReadResult(
                kind="availability",
                ok=True,
                payload={
                    "slots": [
                        {
                            "start_at": start.isoformat(),
                            "end_at": end.isoformat(),
                            "start_local": local_start.isoformat(),
                            "doctor_id": DOCTOR,
                            "doctor_name": "Dr Test",
                            "branch_id": BRANCH,
                            "branch_name": "Main",
                            "laser_device_key": "v1",
                            "laser_device_name": "v1",
                        }
                    ]
                },
            )
        ]
    )
    monkeypatch.setattr(
        "app.services.agent_v2.orchestrator.resolve_additional_services",
        lambda *_args, **_kwargs: [SimpleNamespace(service_id=A, device_key="v1")],
    )

    _prepared, proposal = _prepare_booking_proposal(
        step,
        reads=reads,
        current_task=narrowed,
        db=SimpleNamespace(),
        workspace=SimpleNamespace(id="workspace-1", timezone="Africa/Cairo"),
        patient=SimpleNamespace(id="patient-1"),
        catalog=_catalog(),
        now=NOW,
        turn_id="replacement-proposal",
    )

    assert proposal is not None
    assert proposal.service_ids == [A, C]
    assert proposal.main_service_id == C
    assert proposal.additional_service_ids == [A]
    assert B not in proposal.service_ids
