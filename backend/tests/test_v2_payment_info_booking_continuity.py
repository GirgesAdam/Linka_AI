from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_normalization import normalize_semantic_invariants
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    OptionChoice,
    OptionSnapshot,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def _semantic_context():
    return build_semantic_context(
        {
            "services": [{"id": "svc-hydrafacial", "name": "?????????"}],
            "doctors": [{"id": "doc-1", "name": "?. ????", "service_ids": ["svc-hydrafacial"]}],
            "branches": [{"id": "branch-1", "name": "Tia Clinic"}],
        }
    )


def _booking_task(*, with_slot_snapshot: bool = False) -> BookingTaskState:
    snapshot = None
    if with_slot_snapshot:
        snapshot = OptionSnapshot(
            snapshot_id="booking-options",
            purpose="booking_slot",
            task_version=3,
            created_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=14),
            options=[
                OptionChoice(
                    ref="slot-1",
                    label="5:00 ?????",
                    payload={
                        "service_id": "svc-hydrafacial",
                        "doctor_id": "doc-1",
                        "start_local": "2026-09-29T17:00:00+03:00",
                    },
                )
            ],
        )
    return BookingTaskState(
        status="awaiting_choice" if snapshot is not None else "collecting",
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="booking-start",
            granted_at=NOW - timedelta(minutes=2),
        ),
        constraints=CustomerConstraints(
            service_id="svc-hydrafacial",
            date=DateConstraint(mode="exact", start_date="2026-09-29"),
            time=None,
        ),
        option_snapshot=snapshot,
        version=3,
    )


def _payment_info() -> TurnOperation:
    return TurnOperation(
        type="payment_info",
        entities=TurnEntities(),
        financial_ownership="none",
        execution_intent="informational",
    )


def _context(active_task=None) -> PlannerContext:
    return PlannerContext(
        semantic_context=_semantic_context(),
        active_task=active_task,
        now=NOW,
    )


def test_payment_information_is_a_read_not_a_handoff_or_financial_write() -> None:
    operation = _payment_info()
    turn = normalize_semantic_invariants(TiaTurnUnderstanding(operations=[operation]))

    assert [item.type for item in turn.operations] == ["payment_info"]
    plan = plan_turn(turn, _context())

    assert plan.handoff_category is None
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.disposition == "read"
    assert step.state_action == "none"
    assert step.write_intent is None
    assert [read.kind for read in step.reads] == ["clinic_info"]
    assert step.facts["booking_requires_payment"] is False
    assert step.facts["payment_execution_owner"] == "reception"
    assert step.facts["active_booking_in_progress"] is False


def test_payment_information_preserves_active_booking_context() -> None:
    active = _booking_task()
    operation = _payment_info()
    turn = TiaTurnUnderstanding(operations=[operation])
    plan = plan_turn(turn, _context(active_task=active))
    step = plan.steps[0]

    transition = apply_step_state(
        active,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="payment-side-read",
    )

    assert plan.handoff_category is None
    assert step.facts["active_booking_in_progress"] is True
    assert step.write_intent is None
    assert transition.reason == "side_read_preserved"
    assert transition.changed is False
    assert transition.active_task == active
    assert transition.active_task.constraints.service_id == "svc-hydrafacial"
    assert transition.active_task.constraints.date.start_date == "2026-09-29"


def test_payment_information_preserves_verified_booking_slot_options() -> None:
    active = _booking_task(with_slot_snapshot=True)
    operation = _payment_info()
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        _context(active_task=active),
    ).steps[0]

    transition = apply_step_state(
        active,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="payment-between-options-and-selection",
    )

    assert transition.active_task == active
    assert transition.active_task.option_snapshot is not None
    assert transition.active_task.option_snapshot.snapshot_id == "booking-options"
    assert transition.active_task.option_snapshot.options[0].label == "5:00 ?????"


def test_reception_owned_financial_action_still_normalizes_to_payment_handoff() -> None:
    financial_action = TurnOperation(
        type="payment_info",
        entities=TurnEntities(),
        financial_ownership="reception",
        execution_intent="execute",
    )
    normalized = normalize_semantic_invariants(
        TiaTurnUnderstanding(operations=[financial_action])
    )

    assert [item.type for item in normalized.operations] == ["human_support"]
    plan = plan_turn(normalized, _context(active_task=_booking_task()))
    assert plan.handoff_category == "payment"
    assert plan.steps[-1].disposition == "handoff"
    assert plan.steps[-1].write_intent is None
    assert plan.steps[-1].facts["preserve_active_task"] is True


def test_explicit_human_request_about_payment_remains_handoff() -> None:
    handoff = TurnOperation(
        type="human_support",
        entities=TurnEntities(),
        financial_ownership="none",
        execution_intent="informational",
    )
    plan = plan_turn(TiaTurnUnderstanding(operations=[handoff]), _context())

    assert plan.handoff_category == "customer_request"
    assert plan.steps[0].disposition == "handoff"
    assert plan.steps[0].write_intent is None


def test_pulse_financial_ledger_still_routes_to_reception_without_mutation() -> None:
    pulse_financial = TurnOperation(
        type="pulse_info",
        entities=TurnEntities(),
        requested_pulse_details=["financial_ledger"],
        financial_ownership="none",
        execution_intent="informational",
    )
    normalized = normalize_semantic_invariants(
        TiaTurnUnderstanding(operations=[pulse_financial])
    )

    assert [item.type for item in normalized.operations] == ["human_support"]
    plan = plan_turn(normalized, _context())
    assert plan.handoff_category == "payment"
    assert all(step.write_intent is None for step in plan.steps)


def test_safe_pulse_balance_read_remains_agent_owned() -> None:
    pulse_read = TurnOperation(
        type="pulse_info",
        entities=TurnEntities(),
        requested_pulse_details=["balance"],
        financial_ownership="none",
        execution_intent="informational",
    )
    normalized = normalize_semantic_invariants(
        TiaTurnUnderstanding(operations=[pulse_read])
    )
    plan = plan_turn(normalized, _context())

    assert plan.handoff_category is None
    assert plan.steps[0].disposition == "read"
    assert plan.steps[0].write_intent is None
    assert [read.kind for read in plan.steps[0].reads] == ["pulse_balance"]
