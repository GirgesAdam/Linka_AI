from __future__ import annotations

from datetime import UTC, datetime

from app.agents.v2.semantic_context import build_semantic_context, ground_turn_references
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.services.agent_v2.active_task_progress import plan_active_task_progress
from app.services.agent_v2.branch_task_resolution import resolve_scheduling_branch_parameters
from app.services.agent_v2.grouped_booking_continuity import (
    restore_grouped_booking_understanding,
)
from app.services.agent_v2.state import (
    BookingTaskState,
    CustomerConstraints,
    GroupedBookingState,
    WriteAuthorization,
)
from app.services.agent_v2.turn_normalization import expand_multi_service_operations


def _catalog(*, branches: int = 1) -> dict[str, object]:
    branch_rows = [
        {"id": f"branch-{index}", "name": f"Branch {index}", "city": "Cairo"}
        for index in range(1, branches + 1)
    ]
    return {
        "services": [
            {"id": "s1", "name": "إبط", "requires_laser_device": False},
            {"id": "s2", "name": "بكيني", "requires_laser_device": False},
            {"id": "s3", "name": "ذراعين كاملين", "requires_laser_device": False},
        ],
        "branches": branch_rows,
        "doctors": [
            {
                "id": "d1",
                "name": "Doctor",
                "service_ids": ["s1", "s2", "s3"],
                "branch_ids": [row["id"] for row in branch_rows],
            }
        ],
        "appointments": [],
        "packages": [],
    }


def _book(service_ref: str, *, branch_ref: str | None = None, date: bool = False) -> TurnOperation:
    return TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(text=None, ref=service_ref),
            branch=EntityReference(text="branch", ref=branch_ref) if branch_ref else None,
            date=DateConstraint(mode="exact", start_date="2026-10-13") if date else None,
        ),
        execution_intent="execute",
    )


def _authorized() -> WriteAuthorization:
    return WriteAuthorization(
        operation="booking",
        authorized=True,
        source_turn_id="turn-1",
        granted_at=datetime(2026, 10, 11, tzinfo=UTC),
    )


def test_semantic_context_exposes_and_grounds_branch_refs() -> None:
    context = build_semantic_context(_catalog(branches=2))
    assert [row["name"] for row in context.model_input["branches"]] == ["Branch 1", "Branch 2"]
    turn = TiaTurnUnderstanding(
        operations=[_book("S1", branch_ref="B2")],
        safety_signals=[],
    )
    grounded = ground_turn_references(turn, context)
    assert grounded.operations[0].entities.branch is not None
    assert grounded.operations[0].entities.branch.ref == "B2"
    assert context.resolve("B2", expected_kind="branch") == "branch-2"


def test_multi_service_expansion_preserves_explicit_branch() -> None:
    context = build_semantic_context(_catalog(branches=2))
    operation = TurnOperation(
        type="book",
        entities=TurnEntities(
            service=EntityReference(
                text="إبط وبكيني وذراعين",
                ref=None,
                candidate_refs=["S1", "S2", "S3"],
                candidate_mode="set",
            ),
            branch=EntityReference(text="Branch 2", ref="B2"),
        ),
        execution_intent="execute",
    )
    expanded, groups = expand_multi_service_operations(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        semantic_context=context,
    )
    assert len(expanded.operations) == 3
    assert len(groups) == 3
    assert {item.entities.branch.ref for item in expanded.operations if item.entities.branch} == {"B2"}


def test_single_branch_resolves_deterministically_before_availability() -> None:
    context = build_semantic_context(_catalog(branches=1))
    params, refs, reason = resolve_scheduling_branch_parameters(
        {"service_id": "s1"}, context=context
    )
    assert reason is None
    assert refs == []
    assert params["branch_id"] == "branch-1"


def test_multiple_branches_require_grounded_choice() -> None:
    context = build_semantic_context(_catalog(branches=2))
    params, refs, reason = resolve_scheduling_branch_parameters(
        {"service_id": "s1"}, context=context
    )
    assert params.get("branch_id") is None
    assert reason == "branch_choice"
    assert refs == ["B1", "B2"]


def test_explicit_branch_is_preserved_by_backend_resolution() -> None:
    context = build_semantic_context(_catalog(branches=2))
    params, refs, reason = resolve_scheduling_branch_parameters(
        {"service_id": "s1", "branch_id": "branch-2"}, context=context
    )
    assert reason is None
    assert refs == []
    assert params["branch_id"] == "branch-2"


def test_grouped_date_continuation_single_branch_reaches_availability() -> None:
    context = build_semantic_context(_catalog(branches=1))
    date = DateConstraint(mode="exact", start_date="2026-10-13")
    components = [
        CustomerConstraints(service_id=service_id, date=date)
        for service_id in ("s1", "s2", "s3")
    ]
    task = BookingTaskState(
        write_authorization=_authorized(),
        constraints=components[0],
        grouped=GroupedBookingState(components=components),
    )
    step = plan_active_task_progress(task, operation_index=0, context=context)
    assert step.disposition == "read"
    assert step.clarification_field is None
    assert step.reads[-1].kind == "availability"
    assert step.reads[-1].parameters["branch_id"] == "branch-1"


def test_grouped_date_continuation_multi_branch_clarifies_before_read() -> None:
    context = build_semantic_context(_catalog(branches=3))
    date = DateConstraint(mode="exact", start_date="2026-10-13")
    components = [
        CustomerConstraints(service_id=service_id, date=date)
        for service_id in ("s1", "s2", "s3")
    ]
    task = BookingTaskState(
        write_authorization=_authorized(),
        constraints=components[0],
        grouped=GroupedBookingState(components=components),
    )
    step = plan_active_task_progress(task, operation_index=0, context=context)
    assert step.disposition == "clarify"
    assert step.clarification_field == "branch"
    assert step.reads == []
    assert step.write_intent is None
    assert step.facts["_verified_candidate_refs"] == ["B1", "B2", "B3"]


def test_grouped_restore_applies_continuation_branch_to_every_component() -> None:
    context = build_semantic_context(_catalog(branches=2))
    components = [CustomerConstraints(service_id=value) for value in ("s1", "s2", "s3")]
    task = BookingTaskState(
        write_authorization=_authorized(),
        constraints=components[0],
        grouped=GroupedBookingState(components=components),
    )
    continuation = TurnOperation(
        type="continue_active",
        entities=TurnEntities(
            branch=EntityReference(text="Branch 2", ref="B2"),
            date=DateConstraint(mode="exact", start_date="2026-10-13"),
        ),
        execution_intent="execute",
    )
    restored, groups = restore_grouped_booking_understanding(
        TiaTurnUnderstanding(operations=[continuation], safety_signals=[]),
        active_task=task,
        context=context,
    )
    assert len(restored.operations) == 3
    assert len(groups) == 3
    for operation in restored.operations:
        assert operation.entities.branch is not None
        assert operation.entities.branch.ref == "B2"
        assert operation.entities.date is not None
        assert operation.entities.date.start_date == "2026-10-13"
