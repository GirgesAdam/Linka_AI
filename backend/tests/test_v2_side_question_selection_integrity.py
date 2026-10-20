from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.v2.semantic_context import SemanticContext
from app.agents.v2.turn_contract import (
    Selection,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import (
    adapt_matching_active_task_step,
    plan_active_task_progress,
)
from app.services.agent_v2.orchestrator import (
    _active_task_reference_option_context,
    _active_task_reference_option_is_current,
    _verified_pending_reference_time,
)
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.reference_resolution import build_availability_reference_options
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
SLOT = {
    "branch_id": "branch-1",
    "service_id": "svc-main",
    "doctor_id": "doctor-youssef",
    "doctor_name": "Youssef Samir",
    "laser_device_key": "candela",
    "laser_device_name": "Candela",
    "start_at": "2026-10-13T16:00:00+03:00",
    "start_local": "2026-10-13T16:00:00+03:00",
    "start_time_24h": "16:00",
}
REFERENCE_CONTEXT = {
    "availability_reference_options": [
        {"option_ref": "opt_1", "index": 1, "concrete": True, "slot": SLOT},
    ]
}


def _task(*, expired: bool = False) -> BookingTaskState:
    expires_at = NOW - timedelta(seconds=1) if expired else NOW + timedelta(minutes=10)
    return BookingTaskState(
        status="awaiting_choice",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id="svc-main",
            device_key="candela",
            date={"mode": "exact", "start_date": "2026-10-13"},
        ),
        requested_service_ids=["svc-main", "svc-additional"],
        service_device_keys={"svc-main": "candela", "svc-additional": "candela"},
        option_snapshot=OptionSnapshot(
            snapshot_id="snapshot-1",
            purpose="booking_slot",
            task_version=4,
            created_at=NOW - timedelta(minutes=1),
            expires_at=expires_at,
            options=[
                OptionChoice(
                    ref="slot-1",
                    label="16:00 ? Youssef",
                    payload={
                        "branch_id": "branch-1",
                        "service_id": "svc-main",
                        "doctor_id": "doctor-youssef",
                        "doctor_name": "Youssef Samir",
                        "device_key": "candela",
                        "laser_device_name": "Candela",
                        "start_at": "2026-10-13T16:00:00+03:00",
                        "start_local": "2026-10-13T16:00:00+03:00",
                        "start_time_24h": "16:00",
                    },
                )
            ],
        ),
        version=4,
    )


def _selection_operation() -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(),
        selection=Selection(kind="ref", ref="opt_1"),
        execution_intent="execute",
        active_task_relationship="continue",
        continues_previous=True,
    )


def test_current_reference_option_matches_current_booking_snapshot() -> None:
    assert _active_task_reference_option_is_current(
        _task(),
        option_ref="opt_1",
        availability_context=REFERENCE_CONTEXT,
        now=NOW,
    ) is True


def test_expired_reference_option_fails_closed() -> None:
    assert _active_task_reference_option_is_current(
        _task(expired=True),
        option_ref="opt_1",
        availability_context=REFERENCE_CONTEXT,
        now=NOW,
    ) is False


def test_reference_option_must_match_current_snapshot_payload() -> None:
    mismatched = {
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": True,
                "slot": {**SLOT, "doctor_id": "doctor-other"},
            }
        ]
    }
    assert _active_task_reference_option_is_current(
        _task(),
        option_ref="opt_1",
        availability_context=mismatched,
        now=NOW,
    ) is False


def test_explicit_time_narrows_current_compressed_window_to_one_snapshot_slot() -> None:
    compressed = {
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "index": 1,
                "concrete": False,
                "start_local": "2026-10-13T16:00:00+03:00",
                "end_local": "2026-10-13T22:00:00+03:00",
                "start_time_24h": "16:00",
                "end_time_24h": "22:00",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
            }
        ]
    }
    resolved = _active_task_reference_option_context(
        _task(),
        option_ref="opt_1",
        availability_context=compressed,
        explicit_user_time="16:00",
        now=NOW,
    )
    assert resolved is not None
    option = resolved["availability_reference_options"][0]
    assert option["concrete"] is True
    assert option["slot"]["doctor_id"] == "doctor-youssef"
    assert option["slot"]["start_time_24h"] == "16:00"
    assert option["slot"]["laser_device_key"] == "candela"


def test_explicit_time_outside_compressed_window_snapshot_fails_closed() -> None:
    compressed = {
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "concrete": False,
                "start_local": "2026-10-13T16:00:00+03:00",
                "end_local": "2026-10-13T22:00:00+03:00",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
            }
        ]
    }
    assert _active_task_reference_option_context(
        _task(),
        option_ref="opt_1",
        availability_context=compressed,
        explicit_user_time="16:15",
        now=NOW,
    ) is None


def test_explicit_time_compressed_window_requires_unique_snapshot_slot() -> None:
    task = _task()
    assert task.option_snapshot is not None
    duplicate = OptionChoice(
        ref="slot-duplicate",
        label="16:00 duplicate",
        payload={
            **task.option_snapshot.options[0].payload,
            "branch_id": "branch-2",
        },
    )
    task = task.model_copy(
        update={
            "option_snapshot": task.option_snapshot.model_copy(
                update={"options": [*task.option_snapshot.options, duplicate]}
            )
        }
    )
    compressed = {
        "availability_reference_options": [
            {
                "option_ref": "opt_1",
                "concrete": False,
                "start_local": "2026-10-13T16:00:00+03:00",
                "end_local": "2026-10-13T22:00:00+03:00",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
            }
        ]
    }
    assert _active_task_reference_option_context(
        task,
        option_ref="opt_1",
        availability_context=compressed,
        explicit_user_time="16:00",
        now=NOW,
    ) is None



def test_active_booking_same_clock_ambiguity_preserves_time_and_asks_only_doctor() -> None:
    task = _task()
    assert task.option_snapshot is not None
    first = dict(task.option_snapshot.options[0].payload)
    second = {
        **first,
        "doctor_id": "doctor-sara",
        "doctor_name": "Sara",
    }
    task = task.model_copy(
        update={
            "option_snapshot": task.option_snapshot.model_copy(
                update={
                    "options": [
                        task.option_snapshot.options[0],
                        OptionChoice(ref="slot-sara", label="16:00 Sara", payload=second),
                    ]
                }
            )
        }
    )
    context = {
        "availability_reference_options": build_availability_reference_options(
            displayed_windows=[
                {
                    "start_local": first["start_local"],
                    "end_local": first["start_local"],
                    "start_time_24h": "16:00",
                    "end_time_24h": "16:00",
                    "laser_device_name": "Candela",
                }
            ],
            verified_slots=[first, second],
        )
    }
    semantic = SemanticContext(model_input={}, reference_map={}, server_metadata={})
    step = plan_turn(
        TiaTurnUnderstanding(operations=[_selection_operation()]),
        PlannerContext(
            semantic_context=semantic,
            active_task=task,
            now=NOW,
            availability_reference_exact_time="16:00",
            availability_reference_context=context,
        ),
    ).steps[0]

    assert step.disposition == "clarify"
    assert step.clarification_field == "doctor"
    assert step.write_intent is None
    assert step.facts["availability_reference_pending_time"] == "16:00"


def test_verified_option_ref_binds_canonical_doctor_time_and_slot_ref() -> None:
    task = _task()
    operation = _selection_operation()
    semantic = SemanticContext(model_input={}, reference_map={}, server_metadata={})
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(
            semantic_context=semantic,
            active_task=task,
            now=NOW,
            availability_reference_context=REFERENCE_CONTEXT,
        ),
    )
    planned = plan.steps[0]
    adapted = adapt_matching_active_task_step(
        planned,
        operation=operation,
        active_task=task,
        context=semantic,
        now=NOW,
    )
    assert adapted.state_action == "update_active"
    assert adapted.facts["doctor_id"] == "doctor-youssef"
    assert adapted.facts["time"]["start_time"] == "16:00"
    assert adapted.facts["selected_option_ref"] == "opt_1"

    transition = apply_step_state(
        task,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-2",
    )
    assert transition.active_task is not None
    assert transition.active_task.constraints.doctor_id == "doctor-youssef"
    assert transition.active_task.constraints.time is not None
    assert transition.active_task.constraints.time.start_time == "16:00"
    assert transition.active_task.derived.selected_slot_ref == "opt_1"
    assert transition.active_task.requested_service_ids == ["svc-main", "svc-additional"]
    assert transition.active_task.constraints.device_key == "candela"


def test_unknown_ref_never_binds_or_mutates_active_booking() -> None:
    task = _task()
    operation = _selection_operation().model_copy(
        update={"selection": Selection(kind="ref", ref="opt_missing")}
    )
    semantic = SemanticContext(model_input={}, reference_map={}, server_metadata={})
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(
            semantic_context=semantic,
            active_task=task,
            now=NOW,
            availability_reference_context=REFERENCE_CONTEXT,
        ),
    ).steps[0]
    assert step.disposition == "clarify"
    assert step.state_action == "none"
    assert step.facts["availability_reference_reason"] == "unavailable"


def test_verified_doctor_window_narrowing_preserves_only_matching_current_slots() -> None:
    from app.services.agent_v2.orchestrator import _active_task_reference_window_facts
    from app.services.agent_v2.planner import PlanStep

    task = _task()
    assert task.option_snapshot is not None
    maryam = OptionChoice(
        ref="slot-maryam",
        label="16:00 · Maryam",
        payload={
            **task.option_snapshot.options[0].payload,
            "doctor_id": "doctor-maryam",
            "doctor_name": "Maryam Hassan",
        },
    )
    task = task.model_copy(
        update={
            "option_snapshot": task.option_snapshot.model_copy(
                update={"options": [maryam, *task.option_snapshot.options]}
            )
        }
    )
    compressed = {
        "availability_reference_options": [
            {
                "option_ref": "opt_youssef",
                "index": 2,
                "concrete": False,
                "start_local": "2026-10-13T16:00:00+03:00",
                "end_local": "2026-10-13T22:00:00+03:00",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
            }
        ]
    }
    facts = _active_task_reference_window_facts(
        task,
        option_ref="opt_youssef",
        availability_context=compressed,
        now=NOW,
    )
    assert facts is not None
    assert facts["doctor_id"] == "doctor-youssef"

    step = PlanStep(
        operation_index=0,
        operation_type="continue_active",
        disposition="state_update",
        state_action="update_active",
        facts={
            **facts,
            "verified_availability_window_narrowing": True,
            "verified_availability_reference": True,
            "availability_reference_option_ref": "opt_youssef",
        },
    )
    operation = TurnOperation(
        type="continue_active",
        entities=TurnEntities(),
        execution_intent="execute",
        active_task_relationship="continue",
        continues_previous=True,
    )
    adapted = adapt_matching_active_task_step(
        step,
        operation=operation,
        active_task=task,
        context=SemanticContext(model_input={}, reference_map={}, server_metadata={}),
        now=NOW,
    )
    assert adapted.facts["doctor_id"] == "doctor-youssef"
    assert adapted.facts["verified_availability_window_narrowing"] is True

    transition = apply_step_state(
        task,
        step=adapted,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="turn-window",
    )
    updated = transition.active_task
    assert isinstance(updated, BookingTaskState)
    assert updated.constraints.doctor_id == "doctor-youssef"
    assert updated.option_snapshot is not None
    assert updated.option_snapshot.is_current(task_version=updated.version, now=NOW)
    assert updated.option_snapshot.options
    assert {
        choice.payload.get("doctor_id") for choice in updated.option_snapshot.options
    } == {"doctor-youssef"}
    assert updated.constraints.device_key == "candela"
    assert updated.requested_service_ids == ["svc-main", "svc-additional"]


def test_ambiguous_explicit_time_is_carried_only_when_multiple_current_displayed_slots_share_it() -> None:
    task = _task()
    assert task.option_snapshot is not None
    maryam = OptionChoice(
        ref="slot-maryam",
        label="16:00 ? Maryam",
        payload={
            **task.option_snapshot.options[0].payload,
            "doctor_id": "doctor-maryam",
            "doctor_name": "Maryam Hassan",
        },
    )
    task = task.model_copy(
        update={
            "option_snapshot": task.option_snapshot.model_copy(
                update={"options": [maryam, *task.option_snapshot.options]}
            )
        }
    )
    displayed = {
        "availability_reference_options": [
            {
                "option_ref": "opt_maryam",
                "concrete": False,
                "start_local": "2026-10-13T16:00:00+03:00",
                "end_local": "2026-10-13T16:15:00+03:00",
                "doctor_name": "Maryam Hassan",
                "laser_device_name": "Candela",
            },
            {
                "option_ref": "opt_youssef",
                "concrete": False,
                "start_local": "2026-10-13T16:00:00+03:00",
                "end_local": "2026-10-13T16:15:00+03:00",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
            },
        ]
    }
    assert _verified_pending_reference_time(
        task,
        availability_context=displayed,
        exact_time="16:00",
        now=NOW,
    ) == "16:00"
    assert _verified_pending_reference_time(
        task,
        availability_context=displayed,
        exact_time="16:15",
        now=NOW,
    ) is None
    assert _verified_pending_reference_time(
        _task(expired=True),
        availability_context=displayed,
        exact_time="16:00",
        now=NOW,
    ) is None


def test_carried_pending_time_materializes_selected_doctor_window_to_one_current_slot() -> None:
    compressed = {
        "pending_exact_time": "16:00",
        "availability_reference_options": [
            {
                "option_ref": "opt_youssef",
                "concrete": False,
                "start_local": "2026-10-13T16:00:00+03:00",
                "end_local": "2026-10-13T16:15:00+03:00",
                "doctor_name": "Youssef Samir",
                "laser_device_name": "Candela",
            }
        ],
    }
    resolved = _active_task_reference_option_context(
        _task(),
        option_ref="opt_youssef",
        availability_context=compressed,
        explicit_user_time=compressed["pending_exact_time"],
        now=NOW,
    )
    assert resolved is not None
    option = resolved["availability_reference_options"][0]
    assert option["concrete"] is True
    assert option["slot"]["doctor_id"] == "doctor-youssef"
    assert option["slot"]["start_time_24h"] == "16:00"


def test_verified_selected_slot_requires_proposal_gate_for_single_service_progress() -> None:
    task = _task()
    task = task.model_copy(
        update={
            "constraints": task.constraints.model_copy(
                update={
                    "branch_id": "branch-1",
                    "doctor_id": "doctor-youssef",
                    "time": TimeConstraint(
                        mode="exact",
                        start_time="16:00",
                        start_time_ambiguity="none",
                    ),
                }
            ),
            "requested_service_ids": [],
            "service_device_keys": {},
            "derived": task.derived.model_copy(
                update={
                    "selected_slot_ref": "opt_youssef",
                    "commercial_basis_presented_key": "svc-main|candela|standalone",
                    "commercial_basis_presented_device_key": "candela",
                }
            ),
        }
    )
    step = plan_active_task_progress(
        task,
        operation_index=0,
        context=SemanticContext(model_input={}, reference_map={}, server_metadata={}),
    )
    assert step.operation_type == "book"
    assert step.write_intent is not None
    assert step.facts["booking_proposal_required"] is True
    assert step.facts["requested_service_ids"] == ["svc-main"]
