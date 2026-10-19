from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.semantic_state_view import with_safe_read_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TimeConstraint,
    TurnEntities,
    TurnOperation,
)
from app.agents.v2.turn_interpreter import merge_verified_read_context
from app.integrations.clinic.base import AppointmentReadResult, AppointmentRecord
from app.services.agent_v2.live_chat import _verified_read_context_from_turn
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionContext, execute_step_reads

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
PATIENT_ID = UUID("22222222-2222-4222-8222-222222222222")
BRANCH_ID = UUID("33333333-3333-4333-8333-333333333333")
SERVICE_ID = "service-prp"
DOCTOR_AHMED = "doctor-ahmed"
DOCTOR_MARIAM = "doctor-mariam"
THIS_WEEK = {
    "mode": "range",
    "start_date": "2026-09-28",
    "end_date": "2026-10-04",
}
NEXT_WEEK = {
    "mode": "range",
    "start_date": "2026-10-05",
    "end_date": "2026-10-11",
}
AFTER_FIVE = {
    "mode": "after",
    "start_time": "17:00",
    "end_time": None,
    "start_time_ambiguity": "none",
    "end_time_ambiguity": "none",
}
BEFORE_FIVE = {
    "mode": "before",
    "start_time": "17:00",
    "end_time": None,
    "start_time_ambiguity": "none",
    "end_time_ambiguity": "none",
}


def _catalog() -> dict[str, object]:
    return {
        "services": [{"id": SERVICE_ID, "name": "PRP"}],
        "doctors": [
            {"id": DOCTOR_AHMED, "name": "دكتور أحمد", "service_ids": [SERVICE_ID]},
            {"id": DOCTOR_MARIAM, "name": "دكتورة مريم", "service_ids": [SERVICE_ID]},
        ],
        "branches": [{"id": str(BRANCH_ID), "name": "Linka Clinic"}],
        "appointments": [],
    }


def _semantic_context():
    return build_semantic_context(_catalog())


def _operation(
    *,
    entities: TurnEntities,
    continues_previous: bool = False,
    cleared: list[str] | None = None,
    operation_type: str = "appointment_list",
) -> TurnOperation:
    return TurnOperation(
        type=operation_type,
        entities=entities,
        execution_intent="informational",
        continues_previous=continues_previous,
        cleared_verified_read_fields=cleared or [],
    )


def _plan(operation: TurnOperation, *, context=None):
    semantic_context = context or _semantic_context()
    understanding = TiaTurnUnderstanding(operations=[operation])
    return understanding, plan_turn(
        understanding,
        PlannerContext(
            semantic_context=semantic_context,
            active_task=None,
            now=NOW,
        ),
    )


def _verified_context_from_plan(understanding, plan):
    return _verified_read_context_from_turn(
        object(),
        workspace=SimpleNamespace(),
        turn=SimpleNamespace(plan=plan, understanding=understanding, traces=()),
    )


def _previous_read(*, doctor_id: str | None = None) -> dict[str, object]:
    context: dict[str, object] = {
        "operation_type": "appointment_list",
        "service_id": SERVICE_ID,
        "date": dict(THIS_WEEK),
        "time": dict(AFTER_FIVE),
    }
    if doctor_id is not None:
        context["doctor_id"] = doctor_id
    return context


def _context_with_previous(*, doctor_id: str | None = None):
    return with_safe_read_context(
        _semantic_context(),
        read_context=_previous_read(doctor_id=doctor_id),
    )


def _appointment(appointment_id: str, local_date: str) -> AppointmentRecord:
    start_local = datetime.fromisoformat(f"{local_date}T14:00:00+03:00")
    start = start_local.astimezone(UTC)
    return AppointmentRecord(
        appointment_id=appointment_id,
        patient_id=str(PATIENT_ID),
        status="confirmed",
        service_id=SERVICE_ID,
        service_name="PRP",
        branch_id=str(BRANCH_ID),
        branch_name="Linka Clinic",
        doctor_id=DOCTOR_AHMED,
        doctor_name="دكتور أحمد",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        timezone="Africa/Cairo",
        price_minor=100_000,
        currency="EGP",
        payment_status="unpaid",
        billing_context="standard",
    )


class _AppointmentAdapter:
    def __init__(self, appointments: list[AppointmentRecord]) -> None:
        self.appointments = tuple(appointments)

    def require_capability(self, _capability) -> None:
        return None

    def get_patient_appointments(self, _request):
        return AppointmentReadResult(appointments=self.appointments)


def _execute_appointment_read(plan, appointments: list[AppointmentRecord]):
    return execute_step_reads(
        plan.steps[0],
        ReadExecutionContext(
            db=SimpleNamespace(),
            workspace=SimpleNamespace(
                id=WORKSPACE_ID,
                name="Linka Clinic",
                timezone="Africa/Cairo",
                primary_branch_id=BRANCH_ID,
            ),
            patient=SimpleNamespace(id=PATIENT_ID),
            now=NOW,
            catalog=_catalog(),
            adapter=_AppointmentAdapter(appointments),
        ),
    )


def test_appointment_list_persists_actual_verified_read_scope_not_empty_step_facts() -> None:
    operation = _operation(
        entities=TurnEntities(
            service=EntityReference(ref="S1"),
            date=DateConstraint.model_validate(THIS_WEEK),
            time=TimeConstraint.model_validate(AFTER_FIVE),
        )
    )
    understanding, plan = _plan(operation)

    assert plan.steps[0].facts == {}
    assert plan.steps[0].reads[0].parameters == {
        "service_id": SERVICE_ID,
        "date": THIS_WEEK,
        "time": AFTER_FIVE,
    }

    persisted = _verified_context_from_plan(understanding, plan)
    assert persisted == {
        "operation_type": "appointment_list",
        "service_id": SERVICE_ID,
        "date": THIS_WEEK,
        "time": AFTER_FIVE,
    }


def test_original_f3_time_refinement_preserves_week_and_excludes_next_week_row() -> None:
    previous = _previous_read()
    context = with_safe_read_context(_semantic_context(), read_context=previous)
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                entities=TurnEntities(time=TimeConstraint.model_validate(BEFORE_FIVE)),
                continues_previous=True,
            )
        ]
    )

    merged = merge_verified_read_context(turn, context)
    plan = plan_turn(
        merged,
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )
    params = plan.steps[0].reads[0].parameters
    assert params["service_id"] == SERVICE_ID
    assert params["date"] == THIS_WEEK
    assert params["time"] == BEFORE_FIVE

    bundle = _execute_appointment_read(
        plan,
        [
            _appointment("appointment-this-week", "2026-10-02"),
            _appointment("appointment-next-week", "2026-10-09"),
        ],
    )
    rows = bundle.results[0].payload["appointments"]
    assert [row["appointment_id"] for row in rows] == ["appointment-this-week"]


def test_explicit_next_week_date_replaces_previous_week() -> None:
    context = _context_with_previous()
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                entities=TurnEntities(
                    date=DateConstraint.model_validate(NEXT_WEEK),
                    time=TimeConstraint.model_validate(BEFORE_FIVE),
                ),
                continues_previous=True,
            )
        ]
    )

    merged = merge_verified_read_context(turn, context)
    _, plan = _plan(merged.operations[0], context=context)
    params = plan.steps[0].reads[0].parameters

    assert params["date"] == NEXT_WEEK
    assert params["time"] == BEFORE_FIVE
    assert params["service_id"] == SERVICE_ID


def test_unrelated_next_appointment_query_does_not_inherit_temporal_scope() -> None:
    context = _context_with_previous()
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                entities=TurnEntities(),
                continues_previous=False,
            )
        ]
    )

    merged = merge_verified_read_context(turn, context)
    _, plan = _plan(merged.operations[0], context=context)

    assert plan.steps[0].reads[0].parameters == {}


def test_explicit_current_date_wins_over_previous_range() -> None:
    context = _context_with_previous()
    exact = {"mode": "exact", "start_date": "2026-10-03", "end_date": None}
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                entities=TurnEntities(date=DateConstraint.model_validate(exact)),
                continues_previous=True,
            )
        ]
    )

    merged = merge_verified_read_context(turn, context)
    _, plan = _plan(merged.operations[0], context=context)
    params = plan.steps[0].reads[0].parameters

    assert params["date"] == exact
    assert params["time"] == AFTER_FIVE


def test_explicit_time_clear_keeps_verified_date_but_removes_old_time() -> None:
    context = _context_with_previous()
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                entities=TurnEntities(),
                continues_previous=True,
                cleared=["time"],
            )
        ]
    )

    merged = merge_verified_read_context(turn, context)
    _, plan = _plan(merged.operations[0], context=context)
    params = plan.steps[0].reads[0].parameters

    assert params["service_id"] == SERVICE_ID
    assert params["date"] == THIS_WEEK
    assert "time" not in params


def test_doctor_correction_preserves_service_and_temporal_scope() -> None:
    context = _context_with_previous(doctor_id=DOCTOR_AHMED)
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                entities=TurnEntities(doctor=EntityReference(ref="D2")),
                continues_previous=True,
            )
        ]
    )

    merged = merge_verified_read_context(turn, context)
    _, plan = _plan(merged.operations[0], context=context)
    params = plan.steps[0].reads[0].parameters

    assert params == {
        "service_id": SERVICE_ID,
        "doctor_id": DOCTOR_MARIAM,
        "date": THIS_WEEK,
        "time": AFTER_FIVE,
    }


def test_temporal_clear_marker_is_bounded_to_appointment_list_not_availability() -> None:
    context = _context_with_previous()
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                operation_type="availability",
                entities=TurnEntities(),
                continues_previous=True,
                cleared=["time"],
            )
        ]
    )

    merged = merge_verified_read_context(turn, context)
    operation = merged.operations[0]

    assert operation.entities.date is not None
    assert operation.entities.time is not None
    assert operation.entities.time.mode == "after"
