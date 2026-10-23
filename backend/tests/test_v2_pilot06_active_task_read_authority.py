from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import HumanMessage

from app.agents.v2 import availability_composer
from app.agents.v2.responder import compose_v2_customer_reply
from app.agents.v2.semantic_context import SemanticContext, build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_read_context, with_safe_task_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import (
    merge_presented_availability_context,
    merge_verified_read_context,
)
from app.services.agent_v2.active_task_progress import adapt_matching_active_task_step
from app.services.agent_v2.outcome_builder import build_step_outcome
from app.services.agent_v2.planner import PlannerContext, PlanStep, plan_turn
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    GroupedBookingState,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state
from app.services.agent_v2.test_harness import V2FixtureEnvironment, execute_fixture_reads
from app.services.agent_v2.turn_normalization import (
    collapse_booking_service_sets,
    expand_multi_service_operations,
)

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
SATURDAY = "2026-10-24"
SUNDAY = "2026-10-25"
TODAY = "2026-10-20"
UNDERARM = "svc-underarm"
BIKINI = "svc-bikini"
FACIAL = "svc-facial"
SARA = "doctor-sara"
MARYAM = "doctor-maryam"
CANDELA = "candela"
BRANCH = "branch-new-cairo"


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {
                "id": UNDERARM,
                "name": "Underarm Laser",
                "requires_laser_device": True,
                "operational_category": "laser",
                "duration_minutes": 15,
                "laser_devices": [{"device_key": CANDELA, "device_name": "Candela"}],
            },
            {
                "id": BIKINI,
                "name": "Bikini Laser",
                "requires_laser_device": True,
                "operational_category": "laser",
                "duration_minutes": 20,
                "laser_devices": [{"device_key": CANDELA, "device_name": "Candela"}],
            },
            {
                "id": FACIAL,
                "name": "Facial",
                "requires_laser_device": False,
                "operational_category": "facial",
                "duration_minutes": 30,
                "laser_devices": [],
            },
        ],
        "branches": [{"id": BRANCH, "name": "New Cairo", "city": "Cairo"}],
        "doctors": [
            {
                "id": SARA,
                "name": "Dr Sara",
                "service_ids": [UNDERARM, BIKINI],
                "branch_ids": [BRANCH],
            },
            {
                "id": MARYAM,
                "name": "Dr Maryam",
                "service_ids": [UNDERARM, BIKINI],
                "branch_ids": [BRANCH],
            },
        ],
        "laser_devices": [{"device_key": CANDELA, "device_name": "Candela"}],
        "appointments": [],
        "packages": [],
    }


def _ref(context: SemanticContext, *, kind: str, canonical_id: str) -> str:
    for ref, target in context.reference_map.items():
        if target.kind == kind and target.canonical_id == canonical_id:
            return ref
    raise AssertionError(f"missing {kind} ref for {canonical_id}")


def _constraints(*, service: str = UNDERARM, doctor: str | None = SARA, clock: str = "18:00") -> CustomerConstraints:
    return CustomerConstraints(
        service_id=service,
        doctor_id=doctor,
        device_key=CANDELA,
        date=DateConstraint(mode="exact", start_date=SATURDAY),
        time=TimeConstraint(mode="after", start_time=clock),
    )


def _booking_state() -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=_constraints(),
        requested_service_ids=[UNDERARM],
        service_device_keys={UNDERARM: CANDELA},
    )


def _grouped_state() -> BookingTaskState:
    common = {
        "doctor_id": SARA,
        "device_key": CANDELA,
        "date": DateConstraint(mode="exact", start_date=SATURDAY),
        "time": TimeConstraint(mode="after", start_time="17:00"),
    }
    components = [
        CustomerConstraints(service_id=UNDERARM, **common),
        CustomerConstraints(service_id=BIKINI, **common),
    ]
    return BookingTaskState(
        write_authorization=WriteAuthorization(operation="booking", authorized=False),
        constraints=CustomerConstraints(service_id=BIKINI, **common),
        requested_service_ids=[UNDERARM, BIKINI],
        service_device_keys={UNDERARM: CANDELA, BIKINI: CANDELA},
        grouped=GroupedBookingState(components=components),
    )


def _reschedule_state() -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=WriteAuthorization(operation="reschedule", authorized=False),
        target=RescheduleTarget(
            appointment_id="apt-1",
            service_id=UNDERARM,
            doctor_id=SARA,
            device_key=CANDELA,
            start_local="2026-10-22T18:00:00+03:00",
        ),
        replacement=_constraints(),
    )


def _context(
    active_task: BookingTaskState | RescheduleTaskState,
    *,
    recent_read: dict[str, object] | None = None,
) -> SemanticContext:
    context = build_semantic_context(_catalog())
    context = with_safe_task_context(
        context,
        active_task=active_task.model_dump(mode="json"),
    )
    if recent_read is not None:
        context = with_safe_read_context(context, read_context=recent_read)
    return context


def _weak_availability(
    context: SemanticContext,
    *,
    service: str | None = None,
    doctor: str | None = None,
    date: DateConstraint | None = None,
    time: TimeConstraint | None = None,
    relationship: str = "continue",
    continues_previous: bool = True,
) -> TurnOperation:
    return TurnOperation(
        type="availability",
        entities=TurnEntities(
            service=(
                EntityReference(ref=_ref(context, kind="service", canonical_id=service))
                if service is not None
                else None
            ),
            doctor=(
                EntityReference(ref=_ref(context, kind="doctor", canonical_id=doctor))
                if doctor is not None
                else None
            ),
            date=date,
            time=time,
        ),
        execution_intent="informational",
        active_task_relationship=relationship,
        continues_previous=continues_previous,
    )


def _pipeline(
    state: BookingTaskState | RescheduleTaskState,
    operation: TurnOperation,
    *,
    recent_read: dict[str, object] | None = None,
):
    context = _context(state, recent_read=recent_read)
    raw = TiaTurnUnderstanding(operations=[operation])
    after_presented = merge_presented_availability_context(raw, context)
    after_verified = merge_verified_read_context(after_presented, context)
    booking_task = state if isinstance(state, BookingTaskState) else None
    collapsed, service_sets = collapse_booking_service_sets(
        after_verified,
        semantic_context=context,
        catalog=_catalog(),
        active_task=booking_task,
    )
    expanded, _groups = expand_multi_service_operations(
        collapsed,
        semantic_context=context,
        booking_service_sets=service_sets,
    )
    plan = plan_turn(
        expanded,
        PlannerContext(
            semantic_context=context,
            active_task=state,
            now=NOW,
            recent_read_context=recent_read,
        ),
    )
    return context, raw, after_presented, after_verified, expanded, plan


def _availability_params(plan) -> list[dict[str, object]]:
    return [
        request.parameters
        for step in plan.steps
        for request in step.reads
        if request.kind == "availability"
    ]



def test_a_single_service_weak_availability_continuation_uses_durable_task_authority() -> None:
    state = _booking_state()
    operation = _weak_availability(_context(state))
    context, raw, after_presented, after_verified, _expanded, plan = _pipeline(state, operation)
    params = _availability_params(plan)
    assert len(params) == 1
    assert params[0]["service_id"] == UNDERARM
    assert params[0]["doctor_id"] == SARA
    assert params[0]["device_key"] == CANDELA
    assert params[0]["date"] == DateConstraint(mode="exact", start_date=SATURDAY).model_dump(mode="json")
    assert params[0]["time"] == TimeConstraint(mode="after", start_time="18:00").model_dump(mode="json")
    assert all(step.write_intent is None for step in plan.steps)
    assert state == _booking_state()
    assert context.model_input["active_task"]["task_type"] == "booking"


def test_b_explicit_time_change_wins_over_durable_time_only() -> None:
    state = _booking_state()
    operation = _weak_availability(
        _context(state),
        time=TimeConstraint(mode="after", start_time="19:00"),
    )
    _context_out, raw, after_presented, after_verified, _expanded, plan = _pipeline(state, operation)
    params = _availability_params(plan)
    assert len(params) == 1
    assert params[0]["service_id"] == UNDERARM
    assert params[0]["doctor_id"] == SARA
    assert params[0]["device_key"] == CANDELA
    assert params[0]["date"]["start_date"] == SATURDAY
    assert params[0]["time"]["mode"] == "after"
    assert params[0]["time"]["start_time"] == "19:00"
    assert all(step.write_intent is None for step in plan.steps)


def test_c_explicit_doctor_change_wins_while_other_durable_scope_survives() -> None:
    state = _booking_state()
    operation = _weak_availability(_context(state), doctor=MARYAM)
    _context_out, raw, after_presented, after_verified, _expanded, plan = _pipeline(state, operation)
    params = _availability_params(plan)
    assert len(params) == 1
    assert params[0]["service_id"] == UNDERARM
    assert params[0]["doctor_id"] == MARYAM
    assert params[0]["device_key"] == CANDELA
    assert params[0]["date"]["start_date"] == SATURDAY
    assert params[0]["time"]["start_time"] == "18:00"
    assert all(step.write_intent is None for step in plan.steps)


def test_d_grouped_weak_continuation_preserves_full_requested_service_set() -> None:
    state = _grouped_state()
    operation = _weak_availability(_context(state))
    _context_out, raw, after_presented, after_verified, expanded, plan = _pipeline(state, operation)
    params = _availability_params(plan)
    assert {row["service_id"] for row in params} == {UNDERARM, BIKINI}
    assert len(expanded.operations) == 2
    for row in params:
        assert row["doctor_id"] == SARA
        assert row["device_key"] == CANDELA
        assert row["date"]["start_date"] == SATURDAY
        assert row["time"]["mode"] == "after"
        assert row["time"]["start_time"] == "17:00"
    assert state.requested_service_ids == [UNDERARM, BIKINI]
    assert all(step.write_intent is None for step in plan.steps)


def test_e_reschedule_weak_availability_continuation_uses_durable_replacement_scope() -> None:
    state = _reschedule_state()
    operation = _weak_availability(_context(state))
    _context_out, raw, after_presented, after_verified, _expanded, plan = _pipeline(state, operation)
    params = _availability_params(plan)
    assert len(params) == 1
    assert params[0]["service_id"] == UNDERARM
    assert params[0]["doctor_id"] == SARA
    assert params[0]["device_key"] == CANDELA
    assert params[0]["date"]["start_date"] == SATURDAY
    assert params[0]["time"]["mode"] == "after"
    assert params[0]["time"]["start_time"] == "18:00"
    assert all(step.write_intent is None for step in plan.steps)
    assert state == _reschedule_state()


def test_f_durable_grouped_scope_outranks_weaker_recent_read() -> None:
    state = _grouped_state()
    recent = {
        "operation_type": "availability",
        "service_id": BIKINI,
        "doctor_id": SARA,
        "device_key": CANDELA,
        "date": DateConstraint(mode="exact", start_date=TODAY).model_dump(mode="json"),
    }
    context = _context(state, recent_read=recent)
    operation = _weak_availability(context)
    _context_out, raw, after_presented, after_verified, expanded, plan = _pipeline(
        state,
        operation,
        recent_read=recent,
    )
    params = _availability_params(plan)
    assert {row["service_id"] for row in params} == {UNDERARM, BIKINI}
    assert len(expanded.operations) == 2
    assert all(row["date"]["start_date"] == SATURDAY for row in params)
    assert all(row["device_key"] == CANDELA for row in params)
    assert all(row["time"]["start_time"] == "17:00" for row in params)
    assert all(step.write_intent is None for step in plan.steps)


def test_g_unrelated_fresh_availability_does_not_inherit_active_task_scope() -> None:
    state = _booking_state()
    context = _context(state)
    operation = _weak_availability(
        context,
        service=FACIAL,
        date=DateConstraint(mode="exact", start_date=SUNDAY),
        relationship="unspecified",
        continues_previous=False,
    )
    _context_out, raw, after_presented, after_verified, _expanded, plan = _pipeline(state, operation)
    params = _availability_params(plan)
    assert len(params) == 1
    assert params[0]["service_id"] == FACIAL
    assert params[0]["date"]["start_date"] == SUNDAY
    assert "doctor_id" not in params[0]
    assert "device_key" not in params[0]
    assert "time" not in params[0]
    assert all(step.write_intent is None for step in plan.steps)
    assert state == _booking_state()


def test_h_existing_explicit_doctor_clear_beats_durable_booking_doctor() -> None:
    state = _booking_state()
    context = _context(state)
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
        cleared_active_task_fields=["doctor"],
    )
    base_step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
    )
    adapted = adapt_matching_active_task_step(
        base_step,
        operation=operation,
        active_task=state,
        context=context,
        now=NOW,
    )
    transition = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="pilot06-clear",
    )
    assert isinstance(transition.active_task, BookingTaskState)
    after = transition.active_task
    assert after.constraints.doctor_id is None
    assert after.constraints.service_id == UNDERARM
    assert after.constraints.device_key == CANDELA
    assert after.constraints.date == state.constraints.date
    assert after.constraints.time == state.constraints.time
    assert adapted.write_intent is None


def test_side_question_preserves_task_without_projecting_scheduling_scope_into_read() -> None:
    state = _booking_state()
    context = _context(state)
    operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(),
        execution_intent="informational",
        active_task_relationship="unspecified",
        continues_previous=False,
    )
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=context, active_task=state, now=NOW),
    )
    assert state == _booking_state()
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.write_intent is None
    assert step.disposition == "read"
    assert all(request.kind != "availability" for request in step.reads)
    for request in step.reads:
        assert "doctor_id" not in request.parameters
        assert "date" not in request.parameters
        assert "time" not in request.parameters


def test_customer_visible_availability_uses_durable_service_and_date(monkeypatch) -> None:
    state = _booking_state()
    operation = _weak_availability(_context(state))
    context, _raw, _presented, _verified, expanded, plan = _pipeline(state, operation)
    step = plan.steps[0]
    env = V2FixtureEnvironment(
        catalog=_catalog(),
        slots=[
            {
                "branch_id": BRANCH,
                "service_id": UNDERARM,
                "service_name": "Underarm Laser",
                "doctor_id": SARA,
                "doctor_name": "Dr Sara",
                "laser_device_key": CANDELA,
                "laser_device_name": "Candela",
                "start_local": f"{SATURDAY}T19:00:00+03:00",
                "end_local": f"{SATURDAY}T19:15:00+03:00",
                "start_at": f"{SATURDAY}T19:00:00+03:00",
            }
        ],
    )
    reads = execute_fixture_reads(step, env)
    outcome = build_step_outcome(
        step,
        turn=expanded,
        semantic_context=context,
        reads=reads,
    )
    monkeypatch.setattr(
        availability_composer,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(RuntimeError("force deterministic test fallback")),
    )
    reply, source = compose_v2_customer_reply(
        clinic_name="Linka Test Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="Show me the available appointment")],
        outcomes=[outcome],
    )
    assert source == "deterministic:availability-contract-fastpath"
    assert "Underarm Laser" in reply
    assert "7 PM" in reply
    assert "Saturday, October 24" in reply
    assert "Bikini" not in reply
    assert "20 October" not in reply
