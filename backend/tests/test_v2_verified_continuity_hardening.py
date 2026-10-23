from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import _normalize_time_semantics_v2
from app.services.agent_v2 import orchestrator as runtime
from app.services.agent_v2.active_task_progress import (
    classify_active_task_lifecycle,
    plan_active_task_progress,
)
from app.services.agent_v2.live_chat import _verified_read_context_from_turn
from app.services.agent_v2.orchestrator import V2OrchestratedTurn, V2RuntimeStepTrace
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.planner import PlanStep, ReadRequest, TurnPlan
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import StateTransition, apply_step_state
from app.services.agent_v2.state_rules import (
    apply_booking_device_change,
    apply_booking_package_usage_change,
    apply_booking_service_change,
    apply_booking_time_change,
)

NOW = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {
                "id": "svc-underarm",
                "name": "Underarm Laser",
                "requires_laser_device": True,
                "laser_devices": [
                    {"device_key": "prime_lase", "device_name": "Prime Lase"},
                    {"device_key": "candela", "device_name": "Candela"},
                ],
            },
            {
                "id": "svc-hydra",
                "name": "Hydrafacial",
                "requires_laser_device": False,
                "laser_devices": [],
            },
        ],
        "doctors": [
            {
                "id": "doc-ahmed",
                "name": "Ahmed",
                "service_ids": ["svc-underarm", "svc-hydra"],
            }
        ],
        "branches": [{"id": "branch-1", "name": "Clinic"}],
        "appointments": [],
    }


def _ref(kind: str, canonical_id: str) -> str:
    context = build_semantic_context(_catalog())
    return next(
        ref
        for ref, target in context.reference_map.items()
        if target.kind == kind and target.canonical_id == canonical_id
    )


def _booking_state() -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            doctor_id="doc-ahmed",
            branch_id="branch-1",
            device_key="prime_lase",
            date=DateConstraint(mode="exact", start_date="2026-10-23"),
            package_usage="use_existing",
        ),
        derived=DerivedBookingState(
            selected_package_id="owned-package-1",
            package_validated=True,
            commercial_basis_presented_key="svc-underarm|prime_lase|package",
        ),
    )


def _package_offer_turn(*, unique: bool) -> V2OrchestratedTurn:
    operation = TurnOperation(
        type="package_info",
        execution_intent="informational",
        entities=TurnEntities(service=EntityReference(ref=_ref("service", "svc-underarm"))),
        requested_package_details=["offers"],
    )
    step = PlanStep(
        operation_index=0,
        operation_type="package_info",
        disposition="read",
        reads=[
            ReadRequest(
                kind="package_offers",
                parameters={"service_id": "svc-underarm", "device_key": "prime_lase"},
            )
        ],
        response_goal="package_information",
        facts={"service_id": "svc-underarm", "device_key": "prime_lase"},
    )
    outcome = TurnOutcome(status="answered", response_goal="package_information")
    verified = (
        {
            "package_offer_id": "offer-6",
            "service_id": "svc-underarm",
            "device_key": "prime_lase",
            "package_sessions": 6,
            "price_minor": 295000,
            "currency": "EGP",
        }
        if unique
        else {}
    )
    trace = V2RuntimeStepTrace(
        operation_index=0,
        operation_type="package_info",
        disposition_before="read",
        disposition_after="read",
        read_kinds=("package_offers",),
        outcome=outcome,
        verified_parameters=verified,
    )
    return V2OrchestratedTurn(
        understanding=TiaTurnUnderstanding(operations=[operation]),
        plan=TurnPlan(steps=[step]),
        traces=(trace,),
        outcomes=(outcome,),
        reply="offer",
        responder_model=None,
        active_task=None,
        persisted_task=None,
        pending_write=None,
    )


def _capture_purchase_operation(
    monkeypatch: pytest.MonkeyPatch,
    operation: TurnOperation,
    *,
    recent_read_context: dict[str, object] | None,
) -> TurnOperation:
    captured: dict[str, TiaTurnUnderstanding] = {}
    monkeypatch.setattr(runtime, "load_active_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "interpret_customer_turn_v2",
        lambda **kwargs: TiaTurnUnderstanding(operations=[operation]),
    )

    def capture_plan(turn: TiaTurnUnderstanding, _context) -> TurnPlan:
        captured["turn"] = turn
        return TurnPlan(
            steps=[
                PlanStep(
                    operation_index=0,
                    operation_type=turn.operations[0].type,
                    disposition="respond",
                    state_action="none",
                    response_goal="clarification",
                )
            ]
        )

    monkeypatch.setattr(runtime, "plan_turn", capture_plan)
    monkeypatch.setattr(runtime, "adapt_matching_active_task_step", lambda step, **kwargs: step)
    monkeypatch.setattr(runtime, "persist_initial_task_intent", lambda step, **kwargs: step)
    monkeypatch.setattr(
        runtime,
        "apply_step_state",
        lambda *args, **kwargs: StateTransition(active_task=None, changed=False, reason="none"),
    )
    monkeypatch.setattr(runtime, "finalize_step_after_state_transition", lambda step, transition: step)
    monkeypatch.setattr(
        runtime,
        "build_step_outcome",
        lambda step, **kwargs: TurnOutcome(status="answered", response_goal="clarification"),
    )
    monkeypatch.setattr(runtime, "compose_v2_customer_reply", lambda **kwargs: ("ok", "test"))

    runtime.orchestrate_v2_turn(
        db=object(),
        workspace=SimpleNamespace(id=uuid4()),
        patient=SimpleNamespace(id=uuid4()),
        conversation_id=uuid4(),
        run_id=uuid4(),
        history=[HumanMessage(content="continue")],
        local_now=NOW,
        timezone_name="Africa/Cairo",
        clinic_name="Clinic",
        catalog=_catalog(),
        turn_id="turn-2",
        recent_read_context=recent_read_context,
    )
    return captured["turn"].operations[0]


def _verified_offer_context() -> dict[str, object]:
    return {
        "operation_type": "package_info",
        "package_offer_id": "offer-6",
        "package_sessions": 6,
        "service_id": "svc-underarm",
        "device_key": "prime_lase",
    }


def test_cf_1_existing_package_explicit_hhmm_restores_current_turn_time() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent="execute",
                continues_previous=True,
                active_task_relationship="continue",
                entities=TurnEntities(),
            )
        ]
    )
    normalized = _normalize_time_semantics_v2(
        turn,
        latest_customer_text="تمام احجزلي الساعة 14:00 من الباكيدج",
    )
    constraint = normalized.operations[0].entities.time
    assert constraint is not None
    assert constraint.mode == "exact"
    assert constraint.start_time == "14:00"
    assert constraint.start_time_ambiguity == "none"

    state = apply_booking_time_change(
        _booking_state(),
        time=constraint,
        clock_source="literal_24h",
    )
    assert state.constraints.package_usage == "use_existing"
    assert state.derived.selected_package_id == "owned-package-1"
    assert state.derived.package_validated is True
    step = plan_active_task_progress(
        state,
        operation_index=0,
        context=build_semantic_context(_catalog()),
    )
    assert step.write_intent is None or any(read.kind == "availability" for read in step.reads)


def test_cf_2_existing_package_fresh_booking_replaces_task_without_package_carry() -> None:
    operation = TurnOperation(
        type="book",
        execution_intent="execute",
        fresh_task=True,
        active_task_relationship="replace",
        entities=TurnEntities(service=EntityReference(ref=_ref("service", "svc-hydra"), text="Hydrafacial")),
    )
    assert classify_active_task_lifecycle(operation, active_task=_booking_state()) == "replace"
    assert operation.package_usage == "unspecified"


def test_cf_3_service_change_invalidates_verified_package_binding() -> None:
    changed = apply_booking_service_change(_booking_state(), service_id="svc-hydra")
    assert changed.constraints.service_id == "svc-hydra"
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False


def test_cf_4_device_change_invalidates_verified_package_binding() -> None:
    changed = apply_booking_device_change(_booking_state(), device_key="candela")
    assert changed.constraints.device_key == "candela"
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False


def test_cf_5_explicit_package_rejection_drops_old_package_authority() -> None:
    changed = apply_booking_package_usage_change(_booking_state(), package_usage="avoid_existing")
    assert changed.constraints.package_usage == "avoid_existing"
    assert changed.derived.selected_package_id is None
    assert changed.derived.package_validated is False


def test_cf_6_awaiting_slot_without_current_hhmm_does_not_invent_time() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                execution_intent="execute",
                continues_previous=True,
                active_task_relationship="continue",
                entities=TurnEntities(),
            )
        ]
    )
    normalized = _normalize_time_semantics_v2(turn, latest_customer_text="تمام، كمل الحجز")
    assert normalized.operations[0].entities.time is None


def test_unique_verified_package_offer_persists_in_existing_read_context() -> None:
    context = _verified_read_context_from_turn(
        None,
        workspace=SimpleNamespace(id="workspace"),
        turn=_package_offer_turn(unique=True),
    )
    assert context is not None
    assert context["package_offer_id"] == "offer-6"
    assert context["package_sessions"] == 6
    assert context["service_id"] == "svc-underarm"
    assert context["device_key"] == "prime_lase"


def test_multiple_package_offers_do_not_gain_continuity_authority() -> None:
    context = _verified_read_context_from_turn(
        None,
        workspace=SimpleNamespace(id="workspace"),
        turn=_package_offer_turn(unique=False),
    )
    assert context is not None
    assert "package_offer_id" not in context
    assert "package_sessions" not in context


def test_cf_7_one_verified_offer_purchase_reference_restores_missing_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="execute",
        continues_previous=True,
        active_task_relationship="continue",
        entities=TurnEntities(),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context=_verified_offer_context(),
    )
    assert continued.entities.package_sessions == 6


def test_cf_8_multiple_offers_ambiguous_purchase_never_auto_selects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="execute",
        continues_previous=True,
        active_task_relationship="continue",
        entities=TurnEntities(),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context={
            "operation_type": "package_info",
            "service_id": "svc-underarm",
            "device_key": "prime_lase",
        },
    )
    assert continued.entities.package_sessions is None


def test_cf_9_explicit_new_sessions_override_previous_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="execute",
        continues_previous=True,
        active_task_relationship="continue",
        entities=TurnEntities(package_sessions=9),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context=_verified_offer_context(),
    )
    assert continued.entities.package_sessions == 9


def test_cf_10_explicit_new_device_drops_previous_offer_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="execute",
        continues_previous=True,
        active_task_relationship="continue",
        entities=TurnEntities(
            device=EntityReference(ref=_ref("device", "candela"), text="Candela")
        ),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context=_verified_offer_context(),
    )
    assert continued.entities.package_sessions is None


def test_cf_11_fresh_task_never_resurrects_old_verified_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="execute",
        continues_previous=True,
        fresh_task=True,
        active_task_relationship="replace",
        entities=TurnEntities(),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context=_verified_offer_context(),
    )
    assert continued.entities.package_sessions is None


def test_cf_12_side_question_preserves_booking_without_write_or_selection() -> None:
    state = _booking_state()
    operation = TurnOperation(
        type="pricing",
        execution_intent="informational",
        entities=TurnEntities(service=EntityReference(ref=_ref("service", "svc-hydra"), text="Hydrafacial")),
    )
    step = PlanStep(
        operation_index=0,
        operation_type="pricing",
        disposition="read",
        reads=[ReadRequest(kind="service_catalog", parameters={"service_id": "svc-hydra"})],
        state_action="none",
        response_goal="answer_price",
    )
    transition = apply_step_state(
        state,
        step=step,
        operation=operation,
        reads=None,
        now=NOW,
        turn_id="side-question",
    )
    assert transition.active_task == state
    assert step.write_intent is None
    assert state.derived.selected_package_id == "owned-package-1"


def test_explicit_service_change_prevents_verified_offer_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="execute",
        continues_previous=True,
        active_task_relationship="continue",
        entities=TurnEntities(
            service=EntityReference(ref=_ref("service", "svc-hydra"), text="Hydrafacial")
        ),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context=_verified_offer_context(),
    )
    assert continued.entities.package_sessions is None


def test_explicit_package_identity_prevents_verified_offer_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="execute",
        continues_previous=True,
        active_task_relationship="continue",
        entities=TurnEntities(package=EntityReference(text="another package")),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context=_verified_offer_context(),
    )
    assert continued.entities.package_sessions is None


def test_informational_package_turn_never_gains_purchase_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = TurnOperation(
        type="buy_package",
        execution_intent="informational",
        continues_previous=True,
        active_task_relationship="continue",
        entities=TurnEntities(),
    )
    continued = _capture_purchase_operation(
        monkeypatch,
        operation,
        recent_read_context=_verified_offer_context(),
    )
    assert continued.entities.package_sessions is None
