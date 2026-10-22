from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_read_context
from app.agents.v2.turn_contract import (
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_verified_read_context
from app.services.agent_v2.active_task_progress import _constraint_parameters
from app.services.agent_v2.package_booking_policy import (
    BookingPackagePolicyError,
    resolve_booking_package,
)
from app.services.agent_v2.planner import PlannerContext, PlanStep, ReadRequest, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionBundle, ReadResult
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    DerivedBookingState,
    WriteAuthorization,
)
from app.services.agent_v2.state_executor import apply_step_state

NOW = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)


def _continuation_context():
    catalog = {
        "services": [
            {
                "id": "svc-underarm",
                "name": "Underarm",
                "laser_devices": [
                    {"device_key": "prime_lase", "device_name": "Prime Lase", "configured": True}
                ],
            },
            {
                "id": "svc-bikini",
                "name": "Bikini",
                "laser_devices": [
                    {"device_key": "prime_lase", "device_name": "Prime Lase", "configured": True}
                ],
            },
        ],
        "doctors": [
            {
                "id": "doc-ahmed",
                "name": "Ahmed Mahmoud",
                "service_ids": ["svc-underarm", "svc-bikini"],
                "branch_ids": ["branch-main"],
            }
        ],
        "branches": [{"id": "branch-main", "name": "Main"}],
    }
    context = build_semantic_context(catalog)
    return with_safe_read_context(
        context,
        read_context={
            "operation_type": "availability",
            "service_id": "svc-underarm",
            "doctor_id": "doc-ahmed",
            "branch_id": "branch-main",
            "device_key": "prime_lase",
            "date": {"mode": "exact", "start_date": "2026-10-24", "end_date": None},
            "availability_option_count": 2,
        },
    )


def test_a1_immediate_booking_inherits_omitted_verified_availability_scope() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    doctor=EntityReference(text="Ahmed Mahmoud", ref="D1"),
                    device=EntityReference(text="Prime Lase", ref="V1"),
                    time=TimeConstraint(mode="exact", start_time="14:00"),
                ),
                execution_intent="execute",
                continues_previous=False,
            )
        ]
    )

    operation = merge_verified_read_context(turn, _continuation_context()).operations[0]

    assert operation.entities.service is not None
    assert operation.entities.service.ref == "S1"
    assert operation.entities.doctor is not None and operation.entities.doctor.ref == "D1"
    assert operation.entities.device is not None and operation.entities.device.ref == "V1"
    assert operation.entities.branch is not None and operation.entities.branch.ref == "B1"
    assert operation.entities.date is not None
    assert operation.entities.date.start_date == "2026-10-24"
    assert operation.entities.time is not None and operation.entities.time.start_time == "14:00"
    assert operation.execution_intent == "execute"
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation]),
        PlannerContext(semantic_context=_continuation_context(), active_task=None, now=NOW),
    )
    assert plan.steps[0].clarification_field != "service"
    assert plan.steps[0].state_action == "start_booking"


def test_a2_explicit_service_replacement_wins_over_verified_read_scope() -> None:
    turn = TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(text="Bikini", ref="S2"),
                    device=EntityReference(text="Prime Lase", ref="V1"),
                    time=TimeConstraint(mode="exact", start_time="14:00"),
                ),
                execution_intent="execute",
                continues_previous=False,
            )
        ]
    )

    operation = merge_verified_read_context(turn, _continuation_context()).operations[0]

    assert operation.entities.service is not None
    assert operation.entities.service.ref == "S2"
    assert operation.entities.service.text == "Bikini"


def _package_state() -> BookingTaskState:
    return BookingTaskState(
        write_authorization=WriteAuthorization(
            operation="booking",
            authorized=True,
            source_turn_id="turn-1",
            granted_at=NOW,
        ),
        constraints=CustomerConstraints(
            service_id="svc-underarm",
            package_usage="use_existing",
        ),
        derived=DerivedBookingState(),
    )


def _package_operation() -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(),
        package_usage="use_existing",
        execution_intent="execute",
        active_task_relationship="continue",
        continues_previous=True,
    )


def _package_step() -> PlanStep:
    return PlanStep(
        operation_index=0,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="customer_packages", parameters={"service_id": "svc-underarm"})],
        state_action="update_active",
        response_goal="package_information",
        facts={
            "service_id": "svc-underarm",
            "booking_commercial_basis_step": True,
            "commercial_basis_key": "svc-underarm|none|package",
        },
    )


def _package_row(package_id: str, *, device_key: str = "prime_lase") -> dict[str, object]:
    return {
        "id": package_id,
        "service_id": "svc-underarm",
        "laser_device_key": device_key,
        "name": f"Package {package_id}",
        "sessions_remaining": 4,
        "effective_status": "active",
    }


def _package_reads(*rows: dict[str, object]) -> ReadExecutionBundle:
    return ReadExecutionBundle(
        results=[ReadResult(kind="customer_packages", ok=True, payload={"packages": list(rows)})]
    )


def test_c1_unique_usable_package_binds_server_identity_and_device() -> None:
    transition = apply_step_state(
        _package_state(),
        step=_package_step(),
        operation=_package_operation(),
        reads=_package_reads(_package_row("pkg-1")),
        now=NOW,
        turn_id="turn-2",
    )

    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.constraints.package_usage == "use_existing"
    assert state.constraints.device_key == "prime_lase"
    assert state.derived.selected_package_id == "pkg-1"
    assert state.derived.package_validated is True
    assert _constraint_parameters(state)["package_id"] == "pkg-1"


def test_c2_multiple_matching_packages_create_choice_and_selection_binds_exact_id() -> None:
    transition = apply_step_state(
        _package_state(),
        step=_package_step(),
        operation=_package_operation(),
        reads=_package_reads(_package_row("pkg-1"), _package_row("pkg-2")),
        now=NOW,
        turn_id="turn-2",
    )
    state = transition.active_task
    assert isinstance(state, BookingTaskState)
    assert state.derived.selected_package_id is None
    assert state.derived.package_validated is False
    assert state.option_snapshot is not None
    assert state.option_snapshot.purpose == "package"
    assert [item.payload["package_id"] for item in state.option_snapshot.options] == ["pkg-1", "pkg-2"]

    selected = state.option_snapshot.options[1]
    selection_step = PlanStep(
        operation_index=0,
        operation_type="select_active",
        disposition="state_update",
        state_action="select_active",
        facts={
            "choice_purpose": "package",
            "selected_option": selected.model_dump(mode="json"),
        },
    )
    selected_transition = apply_step_state(
        state,
        step=selection_step,
        operation=_package_operation(),
        reads=None,
        now=NOW,
        turn_id="turn-3",
    )
    selected_state = selected_transition.active_task
    assert isinstance(selected_state, BookingTaskState)
    assert selected_state.derived.selected_package_id == "pkg-2"
    assert selected_state.derived.package_validated is True


def test_c3_selected_package_is_revalidated_and_cannot_fall_back_to_standalone(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.agent_v2.package_booking_policy.list_patient_packages",
        lambda *_args, **_kwargs: [],
    )
    with pytest.raises(BookingPackagePolicyError):
        resolve_booking_package(
            object(),
            workspace=type("W", (), {"id": "workspace", "timezone": "Africa/Cairo"})(),
            patient=type("P", (), {"id": "patient"})(),
            service_id=__import__("uuid").uuid4(),
            start_at=datetime(2026, 10, 24, 11, 0, tzinfo=UTC),
            device_key="prime_lase",
            package_usage="use_existing",
            requested_package_id=__import__("uuid").uuid4(),
        )


def test_c2_write_boundary_rejects_ambiguous_use_existing_without_exact_package(monkeypatch) -> None:
    rows = [
        type("Pkg", (), {
            "id": __import__("uuid").uuid4(),
            "name": f"P{index}",
            "expires_at": None,
            "purchased_at": NOW,
            "laser_device_key": None,
        })()
        for index in (1, 2)
    ]
    monkeypatch.setattr(
        "app.services.agent_v2.package_booking_policy.list_patient_packages",
        lambda *_args, **_kwargs: rows,
    )
    with pytest.raises(BookingPackagePolicyError, match="exact package selection"):
        resolve_booking_package(
            object(),
            workspace=type("W", (), {"id": "workspace", "timezone": "Africa/Cairo"})(),
            patient=type("P", (), {"id": "patient"})(),
            service_id=__import__("uuid").uuid4(),
            start_at=datetime(2026, 10, 24, 11, 0, tzinfo=UTC),
            device_key=None,
            package_usage="use_existing",
        )
