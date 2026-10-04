from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    persist_initial_task_intent,
    plan_active_task_progress,
)
from app.services.agent_v2.planner import PlanStep, ReadRequest, WriteIntent
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    RescheduleTarget,
    RescheduleTaskState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)


def _context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "svc-underarm",
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela Gentle"}
                    ],
                }
            ],
            "doctors": [],
            "appointments": [
                {
                    "appointment_id": "apt-1",
                    "service_id": "svc-underarm",
                    "status": "confirmed",
                    "start_local": "2026-09-12T19:00:00+03:00",
                },
                {
                    "appointment_id": "apt-2",
                    "service_id": "svc-underarm",
                    "status": "confirmed",
                    "start_local": "2026-09-14T18:00:00+03:00",
                },
            ],
        }
    )


def _continuation_context():
    return build_semantic_context(
        {
            "services": [
                {
                    "id": "svc-underarm",
                    "name": "ليزر إبط",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela Gentle"},
                        {"device_key": "prime_lase", "device_name": "Prime Lase"},
                    ],
                },
                {
                    "id": "svc-hydra",
                    "name": "Hydrafacial",
                    "requires_laser_device": False,
                    "laser_devices": [],
                },
                {
                    "id": "svc-bikini",
                    "name": "ليزر بيكيني",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {"device_key": "candela_gentle", "device_name": "Candela Gentle"}
                    ],
                },
            ],
            "doctors": [
                {
                    "id": "doc-maryam",
                    "name": "Dr Maryam",
                    "service_ids": ["svc-underarm", "svc-bikini"],
                },
                {
                    "id": "doc-nour",
                    "name": "Dr Nour",
                    "service_ids": ["svc-underarm"],
                },
                {
                    "id": "doc-hydra",
                    "name": "Dr Hydra",
                    "service_ids": ["svc-hydra"],
                },
            ],
            "appointments": [],
        }
    )


def _booking_state(
    *,
    service_id: str = "svc-underarm",
    doctor_id: str | None = "doc-maryam",
    device_key: str | None = "candela_gentle",
) -> BookingTaskState:
    return BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(
            service_id=service_id,
            doctor_id=doctor_id,
            device_key=device_key,
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
            time=TimeConstraint(mode="exact", start_time="19:00"),
            package_usage="use_existing",
        ),
    )


def _state_update_step(operation_type: str = "continue_active") -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type=operation_type,
        disposition="state_update",
        state_action="update_active",
        response_goal="clarification",
    )


def _apply_followup(
    state: BookingTaskState,
    operation: TurnOperation,
    *,
    context=None,
) -> BookingTaskState:
    semantic_context = context or _continuation_context()
    adapted = adapt_matching_active_task_step(
        _state_update_step(operation.type),
        operation=operation,
        active_task=state,
        context=semantic_context,
    )
    transition = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-followup",
    )
    assert isinstance(transition.active_task, BookingTaskState)
    return transition.active_task


def _authorization(operation: str = "booking") -> WriteAuthorization:
    return WriteAuthorization(
        operation=operation,
        authorized=True,
        source_turn_id="turn-start",
        granted_at=NOW,
    )


def _reschedule_state() -> RescheduleTaskState:
    return RescheduleTaskState(
        write_authorization=_authorization("reschedule"),
        target=RescheduleTarget(
            appointment_id="apt-1",
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            start_local="2026-09-12T19:00:00+03:00",
        ),
        replacement=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-maryam",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
        ),
    )


def _fresh_reschedule_step() -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="reschedule",
        disposition="read",
        reads=[ReadRequest(kind="appointments"), ReadRequest(kind="availability")],
        write_intent=WriteIntent(kind="reschedule", authorized=True, parameters={}),
        state_action="start_reschedule",
        response_goal="present_availability",
    )


def test_initial_booking_clarification_is_marked_for_state_persistence() -> None:
    context = _context()
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(service=EntityReference(ref="S1")),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        clarification_field="date",
        response_goal="clarification",
    )

    persisted = persist_initial_task_intent(step, operation=operation, context=context)

    assert persisted.state_action == "start_booking"
    assert persisted.facts["service_id"] == "svc-underarm"


def test_booking_progress_presents_verified_device_prices_before_date() -> None:
    state = BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(service_id="svc-underarm"),
    )

    step = plan_active_task_progress(state, operation_index=0, context=_context())

    assert step.disposition == "read"
    assert [read.kind for read in step.reads] == ["service_catalog"]
    assert step.reads[0].parameters == {"service_id": "svc-underarm"}
    assert step.response_goal == "answer_price"
    assert step.state_action == "update_active"
    assert step.facts["booking_device_price_step"] is True


def test_booking_progress_reads_availability_once_service_and_date_are_known() -> None:
    state = BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
            time=TimeConstraint(mode="after", start_time="18:00"),
        ),
        derived=DerivedBookingState(
            commercial_basis_presented_device_key="candela_gentle"
        ),
    )

    step = plan_active_task_progress(state, operation_index=0, context=_context())

    assert step.disposition == "read"
    assert step.operation_type == "book"
    assert step.state_action == "update_active"
    assert [read.kind for read in step.reads] == ["availability"]
    assert step.write_intent is not None
    assert step.write_intent.kind == "booking"
    assert step.write_intent.authorized is True
    assert step.facts["exact_time_requested"] is False
    assert step.facts["service_requires_laser_device"] is True


def test_exact_time_is_planned_for_verification_not_immediate_success() -> None:
    state = BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            device_key="candela_gentle",
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
            time=TimeConstraint(mode="exact", start_time="19:00"),
        ),
        derived=DerivedBookingState(
            commercial_basis_presented_device_key="candela_gentle"
        ),
    )

    step = plan_active_task_progress(state, operation_index=0, context=_context())

    assert step.disposition == "read"
    assert step.facts["exact_time_requested"] is True
    assert step.write_intent is not None
    assert step.write_intent.requires_verification is True


def test_repeated_reschedule_updates_replacement_without_retargeting_appointment() -> None:
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-09-13"),
            time=TimeConstraint(mode="exact", start_time="20:00"),
        ),
    )

    adapted = adapt_matching_active_task_step(
        _fresh_reschedule_step(),
        operation=operation,
        active_task=_reschedule_state(),
        context=_context(),
    )

    assert adapted.disposition == "state_update"
    assert adapted.state_action == "update_active"
    assert adapted.facts["date"]["start_date"] == "2026-09-13"
    assert adapted.facts["time"]["start_time"] == "20:00"
    assert "appointment_id" not in adapted.facts


def test_explicit_same_reschedule_target_still_updates_replacement() -> None:
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            appointment=EntityReference(ref="A1"),
            date=DateConstraint(mode="exact", start_date="2026-09-13"),
        ),
    )

    adapted = adapt_matching_active_task_step(
        _fresh_reschedule_step(),
        operation=operation,
        active_task=_reschedule_state(),
        context=_context(),
    )

    assert adapted.state_action == "update_active"
    assert "appointment_id" not in adapted.facts


def test_explicit_different_reschedule_target_remains_fresh_workflow() -> None:
    operation = TurnOperation(
        type="reschedule",
        entities=TurnEntities(
            appointment=EntityReference(ref="A2"),
            date=DateConstraint(mode="exact", start_date="2026-09-15"),
        ),
    )
    original = _fresh_reschedule_step()

    adapted = adapt_matching_active_task_step(
        original,
        operation=operation,
        active_task=_reschedule_state(),
        context=_context(),
    )

    assert adapted == original
    assert adapted.state_action == "start_reschedule"



def test_repeated_booking_with_continuation_updates_active_task_instead_of_restarting() -> None:
    state = BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-maryam",
        ),
    )
    operation = TurnOperation(
        type="book",
        continues_previous=True,
        entities=TurnEntities(
            date=DateConstraint(mode="exact", start_date="2026-09-12"),
        ),
    )
    original = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        clarification_field="service",
        response_goal="clarification",
    )

    adapted = adapt_matching_active_task_step(
        original,
        operation=operation,
        active_task=state,
        context=_context(),
    )

    assert adapted.disposition == "state_update"
    assert adapted.state_action == "update_active"
    assert adapted.facts["date"]["start_date"] == "2026-09-12"
    assert "service_id" not in adapted.facts
    assert "doctor_id" not in adapted.facts


def test_continue_active_drops_context_only_identity_ref() -> None:
    state = BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(service_id="svc-underarm"),
    )
    operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(service=EntityReference(ref="S1")),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="state_update",
        state_action="update_active",
        facts={"service_id": "svc-other"},
    )

    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=state,
        context=_context(),
    )

    assert "service_id" not in adapted.facts


def test_continue_active_keeps_explicit_identity_change() -> None:
    state = BookingTaskState(
        write_authorization=_authorization(),
        constraints=CustomerConstraints(service_id="svc-underarm"),
    )
    operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(service=EntityReference(ref="S1", text="Underarm laser")),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="state_update",
        state_action="update_active",
        facts={"service_id": "svc-underarm"},
    )

    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=state,
        context=_context(),
    )

    assert adapted.facts["service_id"] == "svc-underarm"


def test_service_change_preserves_date_time_and_clears_incompatible_dependencies() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="book",
        continues_previous=False,
        entities=TurnEntities(service=EntityReference(ref="S2", text="Hydrafacial")),
    )

    changed = _apply_followup(state, operation)

    assert changed.constraints.service_id == "svc-hydra"
    assert changed.constraints.date == state.constraints.date
    assert changed.constraints.time == state.constraints.time
    assert changed.constraints.doctor_id is None
    assert changed.constraints.device_key is None
    assert changed.constraints.package_usage == "use_existing"


def test_doctor_change_preserves_service_device_date_and_time() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="book",
        continues_previous=False,
        entities=TurnEntities(doctor=EntityReference(ref="D2", text="Dr Nour")),
    )

    changed = _apply_followup(state, operation)

    assert changed.constraints.doctor_id == "doc-nour"
    assert changed.constraints.service_id == state.constraints.service_id
    assert changed.constraints.device_key == state.constraints.device_key
    assert changed.constraints.date == state.constraints.date
    assert changed.constraints.time == state.constraints.time


def test_device_change_preserves_service_doctor_date_and_time() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(device=EntityReference(ref="V2", text="Prime Lase")),
    )

    changed = _apply_followup(state, operation)

    assert changed.constraints.device_key == "prime_lase"
    assert changed.constraints.service_id == state.constraints.service_id
    assert changed.constraints.doctor_id == state.constraints.doctor_id
    assert changed.constraints.date == state.constraints.date
    assert changed.constraints.time == state.constraints.time


def test_date_change_preserves_verified_identity_and_time() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(date=DateConstraint(mode="exact", start_date="2026-09-18")),
    )
    changed = _apply_followup(state, operation)

    assert changed.constraints.date == DateConstraint(mode="exact", start_date="2026-09-18")
    assert changed.constraints.service_id == state.constraints.service_id
    assert changed.constraints.doctor_id == state.constraints.doctor_id
    assert changed.constraints.device_key == state.constraints.device_key
    assert changed.constraints.time == state.constraints.time


def test_time_change_preserves_verified_identity_and_date() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(time=TimeConstraint(mode="exact", start_time="20:00")),
    )

    changed = _apply_followup(state, operation)

    assert changed.constraints.time == TimeConstraint(mode="exact", start_time="20:00")
    assert changed.constraints.service_id == state.constraints.service_id
    assert changed.constraints.doctor_id == state.constraints.doctor_id
    assert changed.constraints.device_key == state.constraints.device_key
    assert changed.constraints.date == state.constraints.date


def test_context_only_stale_refs_cannot_replace_verified_current_identity() -> None:
    state = _booking_state(service_id="svc-hydra", doctor_id="doc-hydra", device_key=None)
    operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(
            service=EntityReference(ref="S1"),
            doctor=EntityReference(ref="D1"),
            device=EntityReference(ref="V1"),
            date=DateConstraint(mode="exact", start_date="2026-09-18"),
        ),
    )

    changed = _apply_followup(state, operation)

    assert changed.constraints.service_id == "svc-hydra"
    assert changed.constraints.doctor_id == "doc-hydra"
    assert changed.constraints.device_key is None
    assert changed.constraints.date == DateConstraint(mode="exact", start_date="2026-09-18")


def test_explicit_override_beats_verified_previous_device() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(device=EntityReference(ref="V2", text="Prime Lase")),
    )

    changed = _apply_followup(state, operation)

    assert changed.constraints.device_key == "prime_lase"


def test_side_information_read_preserves_active_booking_and_resume_uses_it() -> None:
    state = _booking_state()
    side_operation = TurnOperation(
        type="pricing",
        entities=TurnEntities(service=EntityReference(ref="S2", text="Hydrafacial")),
    )
    side_step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog")],
        response_goal="answer_price",
    )

    side_transition = apply_step_state(
        state,
        step=side_step,
        operation=side_operation,
        reads=None,
        now=NOW,
        turn_id="turn-side",
    )
    assert side_transition.active_task == state

    resume_operation = TurnOperation(
        type="continue_active",
        continues_previous=True,
        entities=TurnEntities(),
    )
    resumed = _apply_followup(state, resume_operation)
    next_step = plan_active_task_progress(
        resumed,
        operation_index=0,
        context=_continuation_context(),
    )

    assert resumed == state
    assert next_step.disposition == "read"
    assert next_step.facts["service_id"] == "svc-underarm"
    assert next_step.facts["doctor_id"] == "doc-maryam"
    assert next_step.facts["device_key"] == "candela_gentle"
    assert next_step.facts["date"]["start_date"] == "2026-09-12"


def test_cancel_active_is_not_converted_into_booking_preservation() -> None:
    state = _booking_state()
    operation = TurnOperation(type="cancel_active", entities=TurnEntities())
    step = PlanStep(
        operation_index=0,
        operation_type="cancel_active",
        disposition="state_update",
        state_action="cancel_active",
        response_goal="clarification",
    )

    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=state,
        context=_continuation_context(),
    )
    transition = apply_step_state(
        state,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-cancel",
    )

    assert adapted == step
    assert transition.active_task is None


def test_explicit_fresh_booking_after_previous_task_closed_starts_clean() -> None:
    operation = TurnOperation(
        type="book",
        continues_previous=False,
        entities=TurnEntities(service=EntityReference(ref="S2", text="Hydrafacial")),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="clarify",
        state_action="start_booking",
        clarification_field="date",
        response_goal="clarification",
        facts={"service_id": "svc-hydra"},
    )
    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=None,
        context=_continuation_context(),
    )
    transition = apply_step_state(
        None,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-fresh",
    )

    assert adapted == step
    fresh = transition.active_task
    assert isinstance(fresh, BookingTaskState)
    assert fresh.constraints.service_id == "svc-hydra"
    assert fresh.constraints.doctor_id is None
    assert fresh.constraints.device_key is None
    assert fresh.constraints.date is None
    assert fresh.constraints.time is None


def test_service_change_preserves_still_compatible_doctor_and_device() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="book",
        continues_previous=True,
        entities=TurnEntities(service=EntityReference(ref="S3", text="ليزر بيكيني")),
    )

    changed = _apply_followup(state, operation)

    assert changed.constraints.service_id == "svc-bikini"
    assert changed.constraints.doctor_id == "doc-maryam"
    assert changed.constraints.device_key == "candela_gentle"
    assert changed.constraints.date == state.constraints.date
    assert changed.constraints.time == state.constraints.time
