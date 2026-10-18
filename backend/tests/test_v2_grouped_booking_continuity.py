from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import active_task_semantic_view
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2 import state_persistence
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    persist_initial_task_intent,
    prepare_active_task_clock_progress,
)
from app.services.agent_v2.compound_turn_policy import (
    compound_write_group,
    normalize_compound_turn_plan,
)
from app.services.agent_v2.grouped_booking_continuity import (
    enforce_grouped_booking_write_guard,
    explicitly_narrow_grouped_booking_task,
    grouped_booking_task_from_plan,
    restore_grouped_booking_understanding,
)
from app.services.agent_v2.planner import (
    PlannerContext,
    PlanStep,
    TurnPlan,
    WriteIntent,
    plan_turn,
)
from app.services.agent_v2.state import (
    BookingProposal,
    BookingProposalVisit,
    BookingTaskState,
    CustomerConstraints,
    GroupedBookingState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.state_rules import apply_booking_branch_change

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
DATE = DateConstraint(mode="exact", start_date="2026-09-28")
TIME_A = TimeConstraint(mode="exact", start_time="10:00")
TIME_B = TimeConstraint(mode="exact", start_time="11:15")


def _context():
    return build_semantic_context(
        {
            "services": [
                {"id": "svc-a", "name": "Hydrafacial", "requires_laser_device": False},
                {
                    "id": "svc-b",
                    "name": "Underarm Laser",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "prime_lase", "device_name": "Prime Lase"}
                    ],
                },
                {"id": "svc-c", "name": "Deep Cleansing", "requires_laser_device": False},
            ],
            "doctors": [
                {
                    "id": "doc-1",
                    "name": "Dr Maryam",
                    "service_ids": ["svc-a", "svc-b", "svc-c"],
                }
            ],
            "appointments": [],
        }
    )


def _ref(kind: str, canonical_id: str) -> str:
    context = _context()
    return next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == kind and target.canonical_id == canonical_id
    )


def _operation(
    service_id: str,
    *,
    time: TimeConstraint | None = None,
    grouped_booking_action: str = "preserve_group",
) -> TurnOperation:
    device = (
        EntityReference(ref=_ref("device", "prime_lase"))
        if service_id == "svc-b"
        else None
    )
    return TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(ref=_ref("service", service_id)),
            doctor=EntityReference(ref=_ref("doctor", "doc-1")),
            device=device,
            date=DATE,
            time=time,
        ),
        execution_intent="execute",
        grouped_booking_action=grouped_booking_action,
    )


def _facts(service_id: str, time: TimeConstraint) -> dict[str, object]:
    facts: dict[str, object] = {
        "service_id": service_id,
        "doctor_id": "doc-1",
        "date": DATE.model_dump(mode="json"),
        "time": time.model_dump(mode="json"),
        "package_usage": "unspecified",
        "compound_visit_group": "compound:0,1",
        "compound_write_group": "compound:0,1",
        "compound_visit_grouped": True,
    }
    if service_id == "svc-b":
        facts["device_key"] = "prime_lase"
    return facts


def _step(
    index: int,
    service_id: str,
    time: TimeConstraint,
    *,
    with_write: bool = False,
    requested_service_ids: list[str] | None = None,
) -> PlanStep:
    facts = _facts(service_id, time)
    if requested_service_ids is not None:
        facts["requested_service_ids"] = requested_service_ids
    return PlanStep(
        operation_index=index,
        operation_type="book",
        disposition="write_ready" if with_write else "read",
        write_intent=(
            WriteIntent(
                kind="booking",
                authorized=True,
                parameters={
                    key: value
                    for key, value in facts.items()
                    if key
                    in {
                        "service_id",
                        "doctor_id",
                        "device_key",
                        "date",
                        "time",
                        "package_usage",
                    }
                },
                requires_verification=True,
            )
            if with_write
            else None
        ),
        state_action="start_booking",
        response_goal="present_availability",
        facts=facts,
    )


def _grouped_state() -> BookingTaskState:
    components = [
        CustomerConstraints(
            service_id="svc-a",
            doctor_id="doc-1",
            date=DATE,
            time=TIME_A,
        ),
        CustomerConstraints(
            service_id="svc-b",
            doctor_id="doc-1",
            device_key="prime_lase",
            date=DATE,
            time=TIME_B,
        ),
    ]
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=components[0],
        grouped=GroupedBookingState(components=components),
    )


def _ra_g7_restore_context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "underarm",
                    "name": "Underarm Laser",
                    "requires_laser_device": True,
                    "operational_category": "laser",
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela Gentle"},
                        {"device_key": "existing_device", "device_name": "Existing Device"},
                    ],
                },
                {
                    "id": "bikini",
                    "name": "Bikini Laser",
                    "requires_laser_device": True,
                    "operational_category": "laser",
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela Gentle"},
                        {"device_key": "existing_device", "device_name": "Existing Device"},
                    ],
                },
                {
                    "id": "dermatology",
                    "name": "Dermatology",
                    "requires_laser_device": False,
                    "operational_category": "dermatology",
                },
            ],
            "doctors": [
                {
                    "id": "laser-doc",
                    "name": "Laser Doctor",
                    "service_ids": ["underarm", "bikini"],
                    "branch_ids": ["branch-1"],
                },
                {
                    "id": "derm-doc",
                    "name": "Dermatology Doctor",
                    "service_ids": ["dermatology"],
                    "branch_ids": ["branch-1"],
                },
            ],
            "branches": [{"id": "branch-1", "name": "Clinic"}],
            "appointments": [],
        }
    )


def _ra_g7_restore_state(
    *,
    underarm_device: str | None = None,
    bikini_device: str | None = None,
    service_device_keys: dict[str, str] | None = None,
) -> BookingTaskState:
    components = [
        CustomerConstraints(
            service_id="underarm",
            doctor_id="laser-doc",
            branch_id="branch-1",
            device_key=underarm_device,
            date=DATE,
            time=TIME_A,
        ),
        CustomerConstraints(
            service_id="bikini",
            doctor_id="laser-doc",
            branch_id="branch-1",
            device_key=bikini_device,
            date=DATE,
            time=TIME_A,
        ),
        CustomerConstraints(
            service_id="dermatology",
            doctor_id="derm-doc",
            branch_id="branch-1",
            date=DATE,
            time=TIME_A,
        ),
    ]
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-ra-g7",
            granted_at=NOW,
        ),
        constraints=components[0],
        requested_service_ids=["underarm", "bikini", "dermatology"],
        service_device_keys=dict(service_device_keys or {}),
        grouped=GroupedBookingState(components=components),
    )


def _ra_g7_compound_proposal_state(*, incomplete_visits: bool = False) -> BookingTaskState:
    laser_start = datetime.fromisoformat("2026-10-17T18:00:00+03:00")
    laser_end = datetime.fromisoformat("2026-10-17T18:30:00+03:00")
    derm_end = datetime.fromisoformat("2026-10-17T19:00:00+03:00")
    laser_visit = BookingProposalVisit(
        service_ids=["underarm", "bikini"],
        main_service_id="underarm",
        additional_service_ids=["bikini"],
        branch_id="branch-1",
        doctor_id="laser-doc",
        start_at=laser_start,
        end_at=laser_end,
        device_key="candela_gentle",
        additional_service_device_keys={"bikini": "candela_gentle"},
    )
    derm_visit = BookingProposalVisit(
        service_ids=["dermatology"],
        main_service_id="dermatology",
        branch_id="branch-1",
        doctor_id="derm-doc",
        start_at=laser_end,
        end_at=derm_end,
    )
    proposal = BookingProposal(
        proposal_id="proposal-ra-g7-category-split",
        patient_id="patient-1",
        service_ids=["underarm", "bikini", "dermatology"],
        main_service_id="underarm",
        additional_service_ids=["bikini"],
        branch_id="branch-1",
        doctor_id="laser-doc",
        start_at=laser_start,
        end_at=laser_end,
        device_key="candela_gentle",
        additional_service_device_keys={"bikini": "candela_gentle"},
        visits=[laser_visit, derm_visit],
        task_version=1,
        proposal_version=1,
        created_at=NOW,
        expires_at=datetime(2026, 9, 27, 8, 15, tzinfo=UTC),
    )
    state = BookingTaskState(
        status="awaiting_confirmation",
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(
            service_id="bikini",
            doctor_id="laser-doc",
            branch_id="branch-1",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-10-17"),
            time=TimeConstraint(mode="exact", start_time="18:00"),
            time_clock_source="literal_24h",
        ),
        requested_service_ids=["underarm", "bikini", "dermatology"],
        service_device_keys={
            "underarm": "candela_gentle",
            "bikini": "candela_gentle",
        },
        proposal=proposal,
        grouped=None,
    )
    if incomplete_visits:
        malformed = proposal.model_copy(update={"visits": [laser_visit]})
        return state.model_copy(update={"proposal": malformed})
    return state


def _ra_g7_compound_proposal_resume_turn() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    date=DateConstraint(mode="exact", start_date="2026-10-17"),
                    time=TimeConstraint(mode="exact", start_time="18:00"),
                ),
                execution_intent="execute",
                active_task_relationship="continue",
                continues_previous=True,
                fresh_task=False,
                grouped_booking_action="preserve_group",
            )
        ]
    )


def _ra_g7_resume_turn() -> TiaTurnUnderstanding:
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="continue_active",
                entities=TurnEntities(),
                execution_intent="execute",
                active_task_relationship="continue",
                continues_previous=True,
                grouped_booking_action="preserve_group",
            )
        ]
    )


def _restored_device_ids(
    restored: TiaTurnUnderstanding,
    *,
    context,
) -> list[str | None]:
    devices: list[str | None] = []
    for operation in restored.operations:
        entity = operation.entities.device
        devices.append(
            context.resolve(entity.ref, expected_kind="device")
            if entity is not None and entity.ref is not None
            else None
        )
    return devices


def test_ra_g7_grouped_restore_rehydrates_canonical_service_devices() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        service_device_keys={
            "underarm": "candela_gentle",
            "bikini": "candela_gentle",
        }
    )

    restored, groups = restore_grouped_booking_understanding(
        _ra_g7_resume_turn(),
        active_task=state,
        context=context,
    )

    assert len(restored.operations) == 3
    assert set(groups) == {0, 1, 2}
    assert _restored_device_ids(restored, context=context) == [
        "candela_gentle",
        "candela_gentle",
        None,
    ]


def test_explicit_device_choice_updates_all_laser_components_only() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state()
    catalog = {
        "services": [
            {"id": "underarm", "requires_laser_device": True},
            {"id": "bikini", "requires_laser_device": True},
            {"id": "dermatology", "requires_laser_device": False},
        ]
    }
    device_ref = next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "device" and target.canonical_id == "candela_gentle"
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            device=EntityReference(ref=device_ref, text="Candela Gentle")
        ),
        execution_intent="execute",
        active_task_relationship="continue",
        continues_previous=True,
        grouped_booking_action="preserve_group",
    )

    restored, groups = restore_grouped_booking_understanding(
        TiaTurnUnderstanding(operations=[operation]),
        active_task=state,
        context=context,
        catalog=catalog,
    )

    assert len(restored.operations) == 3
    assert set(groups) == {0, 1, 2}
    assert [
        context.resolve(item.entities.service.ref, expected_kind="service")
        for item in restored.operations
    ] == ["underarm", "bikini", "dermatology"]
    assert _restored_device_ids(restored, context=context) == [
        "candela_gentle",
        "candela_gentle",
        None,
    ]
    assert [item.entities.device.text if item.entities.device else None for item in restored.operations] == [
        "Candela Gentle",
        "Candela Gentle",
        None,
    ]


def test_ra_g7_compound_proposal_restore_uses_per_visit_resource_authority() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_compound_proposal_state()

    restored, groups = restore_grouped_booking_understanding(
        _ra_g7_compound_proposal_resume_turn(),
        active_task=state,
        context=context,
    )

    assert len(restored.operations) == 3
    assert set(groups) == {0, 1, 2}
    assert [
        context.resolve(operation.entities.service.ref, expected_kind="service")
        for operation in restored.operations
    ] == ["underarm", "bikini", "dermatology"]
    assert [
        context.resolve(operation.entities.doctor.ref, expected_kind="doctor")
        for operation in restored.operations
    ] == ["laser-doc", "laser-doc", "derm-doc"]
    assert _restored_device_ids(restored, context=context) == [
        "candela_gentle",
        "candela_gentle",
        None,
    ]


def test_ra_g7_compound_proposal_restore_keeps_shared_anchor_not_visit_sequence() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_compound_proposal_state()

    restored, _groups = restore_grouped_booking_understanding(
        _ra_g7_compound_proposal_resume_turn(),
        active_task=state,
        context=context,
    )

    assert [operation.entities.time.start_time for operation in restored.operations] == [
        "18:00",
        "18:00",
        "18:00",
    ]
    assert all(operation.entities.date.start_date == "2026-10-17" for operation in restored.operations)
    assert all(operation.entities.time.start_time != "18:30" for operation in restored.operations)


def test_ra_g7_compound_proposal_restore_fails_closed_on_incomplete_visit_coverage() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_compound_proposal_state(incomplete_visits=True)
    original = _ra_g7_compound_proposal_resume_turn()

    restored, groups = restore_grouped_booking_understanding(
        original,
        active_task=state,
        context=context,
    )

    assert restored == original
    assert groups == {}
    assert len(restored.operations) == 1
    assert restored.operations[0].entities.doctor is None
    assert restored.operations[0].entities.device is None


def test_grouped_restore_preserves_existing_explicit_component_device() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        underarm_device="existing_device",
        service_device_keys={
            "underarm": "candela_gentle",
            "bikini": "candela_gentle",
        },
    )

    restored, _groups = restore_grouped_booking_understanding(
        _ra_g7_resume_turn(),
        active_task=state,
        context=context,
    )

    assert _restored_device_ids(restored, context=context) == [
        "existing_device",
        "candela_gentle",
        None,
    ]


def test_grouped_restore_missing_service_device_mapping_leaves_component_unchanged() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        service_device_keys={"underarm": "candela_gentle"}
    )

    restored, _groups = restore_grouped_booking_understanding(
        _ra_g7_resume_turn(),
        active_task=state,
        context=context,
    )

    assert _restored_device_ids(restored, context=context) == [
        "candela_gentle",
        None,
        None,
    ]


def test_ra_g7_restored_laser_devices_reach_booking_planner_path() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        service_device_keys={
            "underarm": "candela_gentle",
            "bikini": "candela_gentle",
        }
    )
    restored, _groups = restore_grouped_booking_understanding(
        _ra_g7_resume_turn(),
        active_task=state,
        context=context,
    )

    planned = plan_turn(
        restored,
        PlannerContext(semantic_context=context, active_task=state, now=NOW),
    )

    laser_steps = [
        step
        for step in planned.steps
        if step.facts.get("service_id") in {"underarm", "bikini"}
    ]
    assert len(laser_steps) == 2
    assert all(
        any(request.kind == "availability" for request in step.reads)
        for step in laser_steps
    )
    assert all(
        not (len(step.reads) == 1 and step.reads[0].kind == "service_catalog")
        for step in laser_steps
    )



def _ra_g7_ref(context, kind: str, canonical_id: str) -> str:
    return next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == kind and target.canonical_id == canonical_id
    )


def _ra_g7_multi_operation_turn(
    context,
    *,
    laser_device: str | None = None,
) -> TiaTurnUnderstanding:
    laser_device_entity = (
        EntityReference(ref=_ra_g7_ref(context, "device", laser_device))
        if laser_device is not None
        else None
    )
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref=_ra_g7_ref(context, "service", "bikini")),
                    doctor=EntityReference(ref=_ra_g7_ref(context, "doctor", "laser-doc")),
                    branch=EntityReference(ref=_ra_g7_ref(context, "branch", "branch-1")),
                    device=laser_device_entity,
                    date=DATE,
                    time=TIME_A,
                ),
                execution_intent="execute",
                active_task_relationship="continue",
                continues_previous=True,
                grouped_booking_action="preserve_group",
            ),
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(ref=_ra_g7_ref(context, "service", "dermatology")),
                    doctor=EntityReference(ref=_ra_g7_ref(context, "doctor", "derm-doc")),
                    branch=EntityReference(ref=_ra_g7_ref(context, "branch", "branch-1")),
                    date=DATE,
                    time=TIME_A,
                ),
                execution_intent="execute",
                active_task_relationship="continue",
                continues_previous=True,
                grouped_booking_action="preserve_group",
            ),
        ]
    )


def _ra_g7_multi_operation_service_sets() -> dict[int, list[str]]:
    return {0: ["underarm", "bikini"], 1: ["dermatology"]}


def test_ra_g7_multi_operation_restore_projects_common_visit_device() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        service_device_keys={
            "underarm": "candela_gentle",
            "bikini": "candela_gentle",
        }
    )
    restored, groups = restore_grouped_booking_understanding(
        _ra_g7_multi_operation_turn(context),
        active_task=state,
        context=context,
        booking_service_sets=_ra_g7_multi_operation_service_sets(),
    )

    assert len(restored.operations) == 2
    assert groups == {}
    assert [
        context.resolve(operation.entities.service.ref, expected_kind="service")
        for operation in restored.operations
    ] == ["bikini", "dermatology"]
    assert _restored_device_ids(restored, context=context) == [
        "candela_gentle",
        None,
    ]


def test_ra_g7_multi_operation_restore_unblocks_laser_planner_availability() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        service_device_keys={
            "underarm": "candela_gentle",
            "bikini": "candela_gentle",
        }
    )
    restored, _groups = restore_grouped_booking_understanding(
        _ra_g7_multi_operation_turn(context),
        active_task=state,
        context=context,
        booking_service_sets=_ra_g7_multi_operation_service_sets(),
    )

    planned = plan_turn(
        restored,
        PlannerContext(semantic_context=context, active_task=state, now=NOW),
    )
    steps = {str(step.facts.get("service_id")): step for step in planned.steps}
    laser = steps["bikini"]
    dermatology = steps["dermatology"]
    assert any(request.kind == "availability" for request in laser.reads)
    assert not (len(laser.reads) == 1 and laser.reads[0].kind == "service_catalog")
    assert any(request.kind == "availability" for request in dermatology.reads)


def test_multi_operation_restore_fails_closed_on_conflicting_durable_devices() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        underarm_device="existing_device",
        bikini_device="candela_gentle",
    )
    restored, groups = restore_grouped_booking_understanding(
        _ra_g7_multi_operation_turn(context),
        active_task=state,
        context=context,
        booking_service_sets=_ra_g7_multi_operation_service_sets(),
    )

    assert groups == {}
    assert _restored_device_ids(restored, context=context) == [None, None]


def test_multi_operation_restore_preserves_explicit_operation_device() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        service_device_keys={
            "underarm": "candela_gentle",
            "bikini": "candela_gentle",
        }
    )
    restored, groups = restore_grouped_booking_understanding(
        _ra_g7_multi_operation_turn(context, laser_device="existing_device"),
        active_task=state,
        context=context,
        booking_service_sets=_ra_g7_multi_operation_service_sets(),
    )

    assert groups == {}
    assert _restored_device_ids(restored, context=context) == [
        "existing_device",
        None,
    ]


def test_multi_operation_restore_fails_closed_when_visit_member_device_is_missing() -> None:
    context = _ra_g7_restore_context()
    state = _ra_g7_restore_state(
        service_device_keys={"underarm": "candela_gentle"}
    )
    restored, groups = restore_grouped_booking_understanding(
        _ra_g7_multi_operation_turn(context),
        active_task=state,
        context=context,
        booking_service_sets=_ra_g7_multi_operation_service_sets(),
    )

    assert groups == {}
    assert _restored_device_ids(restored, context=context) == [None, None]

def test_missing_date_clarification_retains_group_component_identity() -> None:
    first_source = _operation("svc-a")
    second_source = _operation("svc-c")
    first = first_source.model_copy(
        update={
            "entities": first_source.entities.model_copy(
                update={"date": None, "time": None}
            )
        }
    )
    second = second_source.model_copy(
        update={
            "entities": second_source.entities.model_copy(
                update={"date": None, "time": None}
            )
        }
    )
    understanding = TiaTurnUnderstanding(operations=[first, second])
    planned = plan_turn(
        understanding,
        PlannerContext(semantic_context=_context(), active_task=None, now=NOW),
    )
    normalized = normalize_compound_turn_plan(
        planned,
        catalog={
            "services": [
                {"id": "svc-a", "duration_minutes": 30},
                {"id": "svc-c", "duration_minutes": 30},
            ]
        },
        operation_visit_groups={0: "semantic-group", 1: "semantic-group"},
    )

    task = grouped_booking_task_from_plan(
        normalized,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-missing-date",
    )

    assert task is not None
    assert task.grouped is not None
    assert [component.service_id for component in task.grouped.components] == [
        "svc-a",
        "svc-c",
    ]


def test_read_only_cross_category_preflight_keeps_all_services_in_durable_task() -> None:
    steps = []
    for index, service_id, time in (
        (0, "svc-b", TIME_A),
        (1, "svc-c", TIME_B),
    ):
        base = _step(index, service_id, time)
        facts = {
            **base.facts,
            "compound_visit_independent_category_resources": True,
        }
        steps.append(
            base.model_copy(
                update={
                    "disposition": "read",
                    "state_action": "none",
                    "write_intent": None,
                    "response_goal": "present_availability",
                    "facts": facts,
                }
            )
        )
    plan = TurnPlan(steps=steps)
    understanding = TiaTurnUnderstanding(
        operations=[_operation("svc-b", time=TIME_A), _operation("svc-c", time=TIME_B)]
    )

    task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-read-only-compound-visits",
    )

    assert task is not None
    assert task.grouped is not None
    assert task.requested_service_ids == ["svc-b", "svc-c"]
    assert [component.service_id for component in task.grouped.components] == [
        "svc-b",
        "svc-c",
    ]
    assert task.grouped.components[0].time == TIME_A
    assert task.grouped.components[1].time == TIME_B
    assert task.write_authorization.authorized is True
    assert task.proposal is None


def test_ra_g7_collapsed_step_preserves_complete_service_set_in_new_task() -> None:
    plan = TurnPlan(
        steps=[
            _step(
                0,
                "svc-b",
                TIME_A,
                requested_service_ids=["svc-a", "svc-b", "svc-a"],
            ),
            _step(1, "svc-c", TIME_B),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            _operation("svc-b", time=TIME_A),
            _operation("svc-c", time=TIME_B),
        ]
    )

    task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-ra-g7-service-set",
    )

    assert task is not None
    assert task.grouped is not None
    assert task.requested_service_ids == ["svc-a", "svc-b", "svc-c"]
    assert [component.service_id for component in task.grouped.components] == [
        "svc-a",
        "svc-b",
        "svc-c",
    ]
    laser_pair = task.grouped.components[:2]
    assert all(component.doctor_id == "doc-1" for component in laser_pair)
    assert all(component.date == DATE for component in laser_pair)
    assert all(component.time == TIME_A for component in laser_pair)
    assert all(component.device_key == "prime_lase" for component in laser_pair)


def test_ra_g7_collapsed_step_continuation_preserves_complete_service_set() -> None:
    plan = TurnPlan(
        steps=[
            _step(
                0,
                "svc-b",
                TIME_A,
                requested_service_ids=["svc-a", "svc-b"],
            ),
            _step(1, "svc-c", TIME_B),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            _operation("svc-b", time=TIME_A),
            _operation("svc-c", time=TIME_B),
        ]
    )
    initial = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-ra-g7-initial",
    )
    assert initial is not None

    continued = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=initial,
        now=NOW,
        turn_id="turn-ra-g7-continuation",
    )

    assert continued is not None
    assert continued.requested_service_ids == ["svc-a", "svc-b", "svc-c"]
    assert continued.grouped is not None
    assert [component.service_id for component in continued.grouped.components] == [
        "svc-a",
        "svc-b",
        "svc-c",
    ]


def test_grouped_plan_captures_durable_component_state() -> None:
    plan = TurnPlan(
        steps=[
            _step(0, "svc-a", TIME_A),
            _step(1, "svc-b", TIME_B),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            _operation("svc-a", time=TIME_A),
            _operation("svc-b", time=TIME_B),
        ]
    )

    task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-1",
    )

    assert task is not None
    assert task.grouped is not None
    assert [item.service_id for item in task.grouped.components] == ["svc-a", "svc-b"]
    assert task.requested_service_ids == ["svc-a", "svc-b"]
    assert task.write_authorization.authorized is True



def test_d6_grouped_explicit_pm_preserves_one_grouped_task_and_all_components() -> None:
    explicit_pm = TimeConstraint(mode="exact", start_time="16:15")
    plan = TurnPlan(steps=[
        _step(0, "svc-a", explicit_pm),
        _step(1, "svc-b", explicit_pm),
    ])
    first = _operation("svc-a", time=explicit_pm)
    second = _operation("svc-b", time=explicit_pm)
    understanding = TiaTurnUnderstanding(operations=[first, second])

    task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-d6-explicit-pm",
    )

    assert task is not None
    assert task.grouped is not None
    assert len(task.grouped.components) == 2
    assert [item.service_id for item in task.grouped.components] == ["svc-a", "svc-b"]
    assert {item.time.start_time for item in task.grouped.components if item.time is not None} == {"16:15"}
    laser = next(item for item in task.grouped.components if item.service_id == "svc-b")
    assert laser.device_key == "prime_lase"
    assert task.write_authorization.authorized is True

def test_side_read_preserves_full_group_unchanged() -> None:
    state = _grouped_state()
    pricing = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(ref=_ref("service", "svc-b")),
            device=EntityReference(ref=_ref("device", "prime_lase")),
        ),
        execution_intent="informational",
        requested_service_details=["price"],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        state_action="none",
        response_goal="answer_price",
    )

    transition = apply_step_state(
        state,
        step=step,
        operation=pricing,
        reads=None,
        now=NOW,
        turn_id="turn-2",
    )

    assert transition.reason == "side_read_preserved"
    assert transition.changed is False
    assert transition.active_task == state
    assert transition.active_task is not None
    assert transition.active_task.grouped is not None
    assert len(transition.active_task.grouped.components) == 2


def test_fresh_compound_component_does_not_replace_new_group() -> None:
    state = _grouped_state()
    source = _operation("svc-a")
    operation = source.model_copy(
        update={"fresh_task": True, "fresh_task_explicit_fields": ["service"]}
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        state_action="none",
        response_goal="clarification",
        clarification_field="date",
        facts={
            "service_id": "svc-a",
            "compound_visit_group": "semantic-multi-book:0",
            "compound_visit_grouped": True,
        },
    )

    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=state,
        context=_context(),
        now=NOW,
    )
    assert adapted.state_action == "none"

    persisted = persist_initial_task_intent(
        adapted,
        operation=operation,
        context=_context(),
    )
    assert persisted.state_action == "start_booking"

    transition = apply_step_state(
        state,
        step=persisted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-fresh-group",
    )
    assert transition.active_task is not None
    assert transition.active_task.grouped is not None
    assert [
        component.service_id for component in transition.active_task.grouped.components
    ] == ["svc-a", "svc-b"]


def test_grouped_component_start_does_not_collapse_durable_group() -> None:
    state = _grouped_state()
    operation = _operation("svc-a", time=TIME_A)
    step = _step(0, "svc-a", TIME_A)

    transition = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-grouped-start",
    )

    assert transition.active_task is not None
    assert transition.active_task.grouped is not None
    assert [
        component.service_id for component in transition.active_task.grouped.components
    ] == ["svc-a", "svc-b"]


def test_resume_reconstructs_all_grouped_components() -> None:
    state = _grouped_state()
    resume = TiaTurnUnderstanding(
        operations=[_operation("svc-b", time=TIME_A)]
    )

    restored, groups = restore_grouped_booking_understanding(
        resume,
        active_task=state,
        context=_context(),
    )

    assert len(restored.operations) == 2
    assert set(groups) == {0, 1}
    services = [
        _context().resolve(operation.entities.service.ref, expected_kind="service")
        for operation in restored.operations
        if operation.entities.service is not None
    ]
    assert services == ["svc-a", "svc-b"]
    assert restored.operations[0].entities.time == TIME_A
    assert restored.operations[1].entities.time == TIME_A


def test_semantic_view_exposes_all_grouped_components_as_refs() -> None:
    state = _grouped_state()

    safe = active_task_semantic_view(
        state.model_dump(mode="json"),
        context=_context(),
    )

    grouped = safe["grouped_components"]
    assert isinstance(grouped, list)
    assert len(grouped) == 2
    assert {item["service_ref"] for item in grouped} == {
        _ref("service", "svc-a"),
        _ref("service", "svc-b"),
    }


def test_implicit_component_loss_cannot_authorize_single_write() -> None:
    state = _grouped_state()
    plan = TurnPlan(steps=[_step(0, "svc-b", TIME_A, with_write=True)])

    guarded = enforce_grouped_booking_write_guard(
        plan,
        active_task=state,
        candidate_task=None,
        explicit_narrowing=False,
    )

    step = guarded.steps[0]
    assert step.write_intent is None
    assert step.disposition == "clarify"
    assert step.facts["grouped_continuity_guard_blocked"] is True


def test_explicit_component_removal_can_collapse_to_single_booking() -> None:
    state = _grouped_state()
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "svc-a",
                time=TIME_A,
                grouped_booking_action="remove_other_components",
            )
        ]
    )

    narrowed = explicitly_narrow_grouped_booking_task(
        turn,
        active_task=state,
        context=_context(),
    )

    assert narrowed is not None
    assert narrowed.grouped is None
    assert narrowed.constraints.service_id == "svc-a"
    assert narrowed.write_authorization.authorized is True


def test_explicit_component_removal_narrows_requested_service_set_state() -> None:
    legacy = _grouped_state()
    state = legacy.model_copy(
        update={
            "constraints": legacy.grouped.components[1],
            "grouped": None,
            "requested_service_ids": ["svc-a", "svc-b"],
            "service_device_keys": {"svc-b": "prime_lase"},
        }
    )
    correction = _operation(
        "svc-a",
        time=TIME_A,
        grouped_booking_action="remove_other_components",
    ).model_copy(update={"execution_intent": "informational"})

    narrowed = explicitly_narrow_grouped_booking_task(
        TiaTurnUnderstanding(operations=[correction]),
        active_task=state,
        context=_context(),
    )

    assert narrowed is not None
    assert narrowed.grouped is None
    assert narrowed.requested_service_ids == ["svc-a"]
    assert narrowed.constraints.service_id == "svc-a"
    assert narrowed.service_device_keys == {}
    assert narrowed.proposal is None
    assert narrowed.version == state.version + 1


def test_explicit_subset_correction_removes_stale_group_component() -> None:
    state = _grouped_state()
    assert state.grouped is not None
    third = CustomerConstraints(
        service_id="svc-c",
        doctor_id="doc-1",
        date=DATE,
        time=TIME_B,
    )
    state = state.model_copy(
        update={
            "grouped": GroupedBookingState(
                components=[*state.grouped.components, third]
            )
        }
    )
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "svc-a",
                time=TIME_A,
                grouped_booking_action="remove_other_components",
            ),
            _operation("svc-b", time=TIME_B),
        ]
    )

    narrowed = explicitly_narrow_grouped_booking_task(
        turn,
        active_task=state,
        context=_context(),
    )

    assert narrowed is not None
    assert narrowed.grouped is not None
    assert [
        component.service_id for component in narrowed.grouped.components
    ] == ["svc-a", "svc-b"]
    assert narrowed.constraints.service_id == "svc-a"
    assert narrowed.version == state.version + 1


def test_service_replacement_full_group_becomes_a_plus_c() -> None:
    existing = _grouped_state()
    plan = TurnPlan(
        steps=[
            _step(0, "svc-a", TIME_A),
            _step(1, "svc-c", TIME_B),
        ]
    )
    understanding = TiaTurnUnderstanding(
        operations=[
            _operation("svc-a", time=TIME_A),
            _operation("svc-c", time=TIME_B),
        ]
    )

    updated = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=existing,
        now=NOW,
        turn_id="turn-replace",
    )

    assert updated is not None
    assert updated.grouped is not None
    assert [item.service_id for item in updated.grouped.components] == ["svc-a", "svc-c"]
    assert updated.version == existing.version + 1


def test_unknown_single_component_does_not_silently_replace_group() -> None:
    state = _grouped_state()
    turn = TiaTurnUnderstanding(operations=[_operation("svc-c", time=TIME_A)])

    restored, groups = restore_grouped_booking_understanding(
        turn,
        active_task=state,
        context=_context(),
    )
    assert restored == turn
    assert groups == {}

    plan = TurnPlan(steps=[_step(0, "svc-c", TIME_A, with_write=True)])
    guarded = enforce_grouped_booking_write_guard(
        plan,
        active_task=state,
        candidate_task=None,
        explicit_narrowing=False,
    )
    assert guarded.steps[0].write_intent is None


def test_group_survives_persist_side_read_reload_and_resume_without_llm() -> None:
    initial_turn = TiaTurnUnderstanding(
        operations=[
            _operation("svc-a", time=TIME_A),
            _operation("svc-b", time=TIME_B),
        ]
    )
    initial_plan = TurnPlan(
        steps=[
            _step(0, "svc-a", TIME_A),
            _step(1, "svc-b", TIME_B),
        ]
    )
    task = grouped_booking_task_from_plan(
        initial_plan,
        understanding=initial_turn,
        existing=None,
        now=NOW,
        turn_id="turn-1",
    )
    assert task is not None
    reloaded = BookingTaskState.model_validate_json(task.model_dump_json())
    assert reloaded.grouped is not None
    assert len(reloaded.grouped.components) == 2

    price_operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(
            service=EntityReference(ref=_ref("service", "svc-b")),
            device=EntityReference(ref=_ref("device", "prime_lase")),
        ),
        execution_intent="informational",
        requested_service_details=["price"],
    )
    side_step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        state_action="none",
        response_goal="answer_price",
    )
    preserved = apply_step_state(
        reloaded,
        step=side_step,
        operation=price_operation,
        reads=None,
        now=NOW,
        turn_id="turn-2",
    ).active_task
    assert isinstance(preserved, BookingTaskState)
    after_side_read = BookingTaskState.model_validate_json(
        preserved.model_dump_json()
    )
    assert after_side_read.grouped is not None
    assert len(after_side_read.grouped.components) == 2

    resume = TiaTurnUnderstanding(
        operations=[_operation("svc-b", time=TIME_A)]
    )
    reconstructed, groups = restore_grouped_booking_understanding(
        resume,
        active_task=after_side_read,
        context=_context(),
    )
    assert len(reconstructed.operations) == 2

    planned = plan_turn(
        reconstructed,
        PlannerContext(
            semantic_context=_context(),
            active_task=after_side_read,
            now=NOW,
        ),
    )
    normalized = normalize_compound_turn_plan(
        planned,
        catalog={
            "services": [
                {"id": "svc-a", "duration_minutes": 60},
                {"id": "svc-b", "duration_minutes": 15},
            ]
        },
        operation_visit_groups=groups,
    )
    candidate = grouped_booking_task_from_plan(
        normalized,
        understanding=reconstructed,
        existing=after_side_read,
        now=NOW,
        turn_id="turn-3",
    )
    guarded = enforce_grouped_booking_write_guard(
        normalized,
        active_task=after_side_read,
        candidate_task=candidate,
        explicit_narrowing=False,
    )

    assert len(guarded.steps) == 2
    # Single-service commercial/device gating must not intercept restored grouped writes.
    assert all(step.response_goal == "present_availability" for step in guarded.steps)
    assert all(step.write_intent is not None for step in guarded.steps)
    write_groups = [compound_write_group(step) for step in guarded.steps]
    assert write_groups[0] is not None
    assert write_groups[0] == write_groups[1]
    assert {
        str(step.facts["service_id"])
        for step in guarded.steps
    } == {"svc-a", "svc-b"}


def _grouped_clock_context():
    return build_semantic_context(
        {
            "services": [
                {"id": "svc-a", "name": "Hydrafacial", "requires_laser_device": False},
                {
                    "id": "svc-b",
                    "name": "Underarm Laser",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "prime_lase", "device_name": "Prime Lase"}
                    ],
                },
            ],
            "doctors": [
                {
                    "id": "doc-1",
                    "name": "Dr Maryam",
                    "service_ids": ["svc-a", "svc-b"],
                    "branch_ids": ["branch-1"],
                }
            ],
            "branches": [
                {
                    "id": "branch-1",
                    "name": "Clinic A",
                    "working_hours": [
                        {"weekday": weekday, "start": "00:00", "end": "23:59"}
                        for weekday in range(7)
                    ],
                }
            ],
            "appointments": [],
        }
    )


def test_grouped_1030_pm_survives_planner_state_persistence_and_resolution() -> None:
    semantic_time = TimeConstraint(
        mode="exact",
        start_time="10:30",
        start_time_ambiguity="twelve_hour",
        spoken_period_hint="pm",
    )
    first = _operation("svc-a", time=semantic_time)
    second = _operation("svc-b", time=semantic_time)
    understanding = TiaTurnUnderstanding(operations=[first, second])
    plan = TurnPlan(
        steps=[
            _step(0, "svc-a", semantic_time),
            _step(1, "svc-b", semantic_time),
        ]
    )

    task = grouped_booking_task_from_plan(
        plan,
        understanding=understanding,
        existing=None,
        now=NOW,
        turn_id="turn-grouped-1030-pm",
    )
    assert task is not None
    assert task.grouped is not None
    assert [component.service_id for component in task.grouped.components] == ["svc-a", "svc-b"]
    assert all(component.time is not None for component in task.grouped.components)
    assert {component.time.spoken_period_hint for component in task.grouped.components if component.time} == {"pm"}

    task = task.model_copy(
        update={
            "requested_service_ids": ["svc-a", "svc-b"],
            "service_device_keys": {"svc-b": "prime_lase"},
        }
    )
    payload = state_persistence._task_namespace(task)["active_task"]
    reloaded = BookingTaskState.model_validate(payload)
    assert reloaded.grouped is not None
    assert reloaded.requested_service_ids == ["svc-a", "svc-b"]
    assert reloaded.service_device_keys == {"svc-b": "prime_lase"}
    assert {component.time.spoken_period_hint for component in reloaded.grouped.components if component.time} == {"pm"}

    selected = apply_booking_branch_change(reloaded, branch_id="branch-1")
    prepared, gate = prepare_active_task_clock_progress(
        selected,
        operation_index=0,
        context=_grouped_clock_context(),
    )
    assert gate is None
    assert isinstance(prepared, BookingTaskState)
    assert prepared.constraints.time is not None
    assert prepared.constraints.time.start_time == "22:30"
    assert prepared.constraints.time.spoken_period_hint == "none"
    assert prepared.requested_service_ids == ["svc-a", "svc-b"]
    assert prepared.service_device_keys == {"svc-b": "prime_lase"}
    assert prepared.grouped is not None
    assert [component.service_id for component in prepared.grouped.components] == ["svc-a", "svc-b"]
    assert {component.time.start_time for component in prepared.grouped.components if component.time} == {"22:30"}
    assert {component.time.spoken_period_hint for component in prepared.grouped.components if component.time} == {"none"}


def _grouped_pm_state(clock: str = "10:30") -> BookingTaskState:
    base = _grouped_state()
    assert base.grouped is not None
    spoken = TimeConstraint(
        mode="exact",
        start_time=clock,
        start_time_ambiguity="twelve_hour",
        spoken_period_hint="pm",
    )
    components = [
        component.model_copy(
            update={
                "branch_id": "branch-1",
                "date": None,
                "time": spoken,
                "time_clock_source": "bare_spoken_12h",
            }
        )
        for component in base.grouped.components
    ]
    return base.model_copy(
        update={
            "constraints": components[0],
            "grouped": GroupedBookingState(components=components),
            "requested_service_ids": ["svc-a", "svc-b"],
            "service_device_keys": {"svc-b": "prime_lase"},
        }
    )


def test_grouped_date_only_continuation_preserves_shared_pm_authority_then_resolves() -> None:
    state = _grouped_pm_state()
    date = DateConstraint(mode="exact", start_date="2026-10-15")
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(date=date),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["date"],
        continues_previous=True,
        grouped_booking_action="preserve_group",
    )
    restored, groups = restore_grouped_booking_understanding(
        TiaTurnUnderstanding(operations=[operation]),
        active_task=state,
        context=_grouped_clock_context(),
    )
    assert len(restored.operations) == 2
    assert groups
    assert all(op.entities.time is not None for op in restored.operations)
    assert {
        op.entities.time.start_time for op in restored.operations if op.entities.time
    } == {"10:30"}
    assert {
        op.entities.time.spoken_period_hint for op in restored.operations if op.entities.time
    } == {"pm"}

    planned = plan_turn(
        restored,
        PlannerContext(
            semantic_context=_grouped_clock_context(), active_task=state, now=NOW
        ),
    )
    # Backend clock resolution happens before compound sequencing.  Both grouped
    # service plans must derive the same canonical requested clock first.
    assert {
        step.facts["time"]["start_time"]
        for step in planned.steps
        if isinstance(step.facts.get("time"), dict)
    } == {"22:30"}
    assert all(
        step.facts["time"]["start_time_ambiguity"] == "none"
        for step in planned.steps
        if isinstance(step.facts.get("time"), dict)
    )

def test_grouped_explicit_same_clock_correction_removes_old_pm_for_whole_visit() -> None:
    state = _grouped_pm_state()
    bare = TimeConstraint(
        mode="exact", start_time="10:30", start_time_ambiguity="twelve_hour"
    )
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(time=bare),
        execution_intent="execute",
        active_task_relationship="continue",
        active_task_explicit_fields=["time"],
        continues_previous=True,
        grouped_booking_action="preserve_group",
    )
    restored, groups = restore_grouped_booking_understanding(
        TiaTurnUnderstanding(operations=[operation]),
        active_task=state,
        context=_grouped_clock_context(),
    )
    assert len(restored.operations) == 2
    assert groups
    assert {
        op.entities.time.spoken_period_hint for op in restored.operations if op.entities.time
    } == {"none"}
    assert {
        op.entities.time.start_time for op in restored.operations if op.entities.time
    } == {"10:30"}
