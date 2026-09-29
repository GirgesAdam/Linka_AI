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
from app.integrations.clinic.base import AvailabilityResult, AvailabilitySlot
from app.services.agent_v2.planner import PlannerContext, PlanStep, ReadRequest, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionContext, execute_step_reads

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
PATIENT_ID = UUID("22222222-2222-4222-8222-222222222222")
BRANCH_ID = UUID("33333333-3333-4333-8333-333333333333")
SERVICE_ID = "service-laser"
DOCTOR_1 = "doctor-one"
DOCTOR_2 = "doctor-two"


def _catalog() -> dict[str, object]:
    return {
        "services": [{"id": SERVICE_ID, "name": "ليزر"}],
        "doctors": [
            {"id": DOCTOR_1, "name": "د. سارة", "service_ids": [SERVICE_ID]},
            {"id": DOCTOR_2, "name": "د. مريم", "service_ids": [SERVICE_ID]},
        ],
        "branches": [{"id": str(BRANCH_ID), "name": "Linka Clinic"}],
    }


def _semantic_context():
    return build_semantic_context(_catalog())


def _operation(
    operation_type: str,
    *,
    entities: TurnEntities,
    execution_intent: str = "informational",
    continues_previous: bool = False,
) -> TurnOperation:
    return TurnOperation(
        type=operation_type,
        entities=entities,
        selection=None,
        package_usage="unspecified",
        requested_service_details=[],
        execution_intent=execution_intent,
        continues_previous=continues_previous,
    )


def test_continuation_inherits_verified_scope_but_new_time_replaces_old_time() -> None:
    context = with_safe_read_context(
        _semantic_context(),
        read_context={
            "operation_type": "availability",
            "service_id": SERVICE_ID,
            "doctor_ids": [DOCTOR_1, DOCTOR_2],
            "date": {"mode": "exact", "start_date": "2026-09-15", "end_date": None},
            "time": {
                "mode": "after",
                "start_time": "18:00",
                "end_time": None,
                "start_time_ambiguity": "none",
                "end_time_ambiguity": "none",
            },
        },
    )
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "availability",
                entities=TurnEntities(
                    time=TimeConstraint(mode="before", start_time="16:00", end_time=None)
                ),
                continues_previous=True,
            )
        ],
        safety_signals=[],
    )

    merged = merge_verified_read_context(turn, context).operations[0]

    assert merged.entities.service is not None
    assert merged.entities.service.ref == "S1"
    assert merged.entities.doctor is not None
    assert merged.entities.doctor.candidate_mode == "set"
    assert merged.entities.doctor.candidate_refs == ["D1", "D2"]
    assert merged.entities.date is not None
    assert merged.entities.date.start_date == "2026-09-15"
    assert merged.entities.time is not None
    assert merged.entities.time.mode == "before"
    assert merged.entities.time.start_time == "16:00"


def test_nearest_followup_inherits_previous_exact_time_as_anchor() -> None:
    context = with_safe_read_context(
        _semantic_context(),
        read_context={
            "operation_type": "availability",
            "service_id": SERVICE_ID,
            "date": {"mode": "exact", "start_date": "2026-09-15", "end_date": None},
            "time": {
                "mode": "exact",
                "start_time": "14:07",
                "end_time": None,
                "start_time_ambiguity": "none",
                "end_time_ambiguity": "none",
            },
        },
    )
    turn = TiaTurnUnderstanding(
        operations=[
            _operation(
                "availability",
                entities=TurnEntities(time=TimeConstraint(mode="nearest")),
                continues_previous=True,
            )
        ],
        safety_signals=[],
    )

    merged = merge_verified_read_context(turn, context).operations[0]

    assert merged.entities.time is not None
    assert merged.entities.time.mode == "nearest"
    assert merged.entities.time.start_time == "14:07"
    assert merged.entities.date is not None
    assert merged.entities.date.start_date == "2026-09-15"


def test_requested_doctor_set_is_a_read_scope_not_an_ambiguity() -> None:
    context = _semantic_context()
    operation = _operation(
        "availability",
        entities=TurnEntities(
            service=EntityReference(text=None, ref="S1", candidate_refs=[]),
            doctor=EntityReference(
                text="الاتنين",
                ref=None,
                candidate_refs=["D1", "D2"],
                candidate_mode="set",
            ),
            date=DateConstraint(mode="next_available"),
        ),
    )
    plan = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    )

    step = plan.steps[0]
    assert step.disposition == "read"
    assert step.clarification_field is None
    assert step.write_intent is None
    assert step.reads[0].parameters["doctor_ids"] == [DOCTOR_1, DOCTOR_2]


def test_requested_doctor_set_cannot_authorize_a_booking_write() -> None:
    context = _semantic_context()
    operation = _operation(
        "book",
        entities=TurnEntities(
            service=EntityReference(text=None, ref="S1", candidate_refs=[]),
            doctor=EntityReference(
                text="واحد منهم",
                ref=None,
                candidate_refs=["D1", "D2"],
                candidate_mode="set",
            ),
            date=DateConstraint(mode="exact", start_date="2026-09-15"),
            time=TimeConstraint(mode="exact", start_time="18:00"),
        ),
        execution_intent="execute",
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    ).steps[0]

    assert step.disposition == "clarify"
    assert step.clarification_field == "doctor"
    assert step.write_intent is None


def test_informational_booking_language_never_creates_write_intent() -> None:
    context = _semantic_context()
    operation = _operation(
        "book",
        entities=TurnEntities(
            service=EntityReference(text=None, ref="S1", candidate_refs=[]),
        ),
        execution_intent="informational",
    )
    step = plan_turn(
        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),
        PlannerContext(semantic_context=context, active_task=None, now=NOW),
    ).steps[0]

    assert step.disposition == "read"
    assert step.write_intent is None
    assert [read.kind for read in step.reads] == ["service_catalog"]


class _Adapter:
    def __init__(self) -> None:
        self.requests = []

    def require_capability(self, capability) -> None:
        return None

    def get_availability(self, request):
        self.requests.append(request)
        slots = {
            DOCTOR_1: [
                _slot(DOCTOR_1, 10, 0),
                _slot(DOCTOR_1, 11, 30),
            ],
            DOCTOR_2: [
                _slot(DOCTOR_2, 10, 30),
                _slot(DOCTOR_2, 12, 0),
            ],
        }.get(request.doctor_id, [])
        return AvailabilityResult(
            timezone="Africa/Cairo",
            branch_id=str(BRANCH_ID),
            branch_name="Linka Clinic",
            service_id=SERVICE_ID,
            service_name="ليزر",
            service_duration_minutes=30,
            service_price_minor=50000,
            service_currency="EGP",
            slots=tuple(slots),
        )


def _slot(doctor_id: str, cairo_hour: int, minute: int) -> AvailabilitySlot:
    start = datetime(2026, 9, 15, cairo_hour - 3, minute, tzinfo=UTC)
    return AvailabilitySlot(
        branch_id=str(BRANCH_ID),
        branch_name="Linka Clinic",
        doctor_id=doctor_id,
        doctor_name="د. سارة" if doctor_id == DOCTOR_1 else "د. مريم",
        service_id=SERVICE_ID,
        service_name="ليزر",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        duration_minutes=30,
        price_minor=50000,
        currency="EGP",
    )


def test_nearest_availability_is_chosen_deterministically_across_requested_doctor_set() -> None:
    adapter = _Adapter()
    workspace = SimpleNamespace(
        id=WORKSPACE_ID,
        name="Linka Clinic",
        timezone="Africa/Cairo",
        primary_branch_id=BRANCH_ID,
    )
    patient = SimpleNamespace(id=PATIENT_ID)
    step = PlanStep(
        operation_index=0,
        operation_type="availability",
        disposition="read",
        reads=[
            ReadRequest(
                kind="availability",
                parameters={
                    "service_id": SERVICE_ID,
                    "doctor_ids": [DOCTOR_1, DOCTOR_2],
                    "date": {"mode": "exact", "start_date": "2026-09-15", "end_date": None},
                    "time": {
                        "mode": "nearest",
                        "start_time": "10:20",
                        "end_time": None,
                        "start_time_ambiguity": "none",
                        "end_time_ambiguity": "none",
                    },
                },
            )
        ],
        response_goal="present_availability",
    )

    bundle = execute_step_reads(
        step,
        ReadExecutionContext(
            db=SimpleNamespace(),
            workspace=workspace,
            patient=patient,
            now=NOW,
            catalog=_catalog(),
            adapter=adapter,
        ),
    )

    slots = bundle.results[0].payload["slots"]
    assert len(adapter.requests) == 2
    assert len(slots) == 1
    assert slots[0]["doctor_id"] == DOCTOR_2
    assert slots[0]["start_time_24h"] == "10:30"
