from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

from app.agents.v2.semantic_context import build_semantic_context
from app.agents.v2.turn_contract import (
    DateConstraint,
    EntityReference,
    TiaTurnUnderstanding,
    TurnEntities,
    TurnOperation,
)
from app.integrations.clinic.base import AvailabilityResult, AvailabilitySlot
from app.services.agent_v2.compound_turn_policy import (
    normalize_compound_turn_plan,
)
from app.services.agent_v2.compound_visit_preflight import preflight_compound_visit_plan
from app.services.agent_v2.planner import PlannerContext, plan_turn
from app.services.agent_v2.read_executor import ReadExecutionContext
from app.services.agent_v2.turn_normalization import (
    collapse_booking_service_sets,
    merge_same_visit_booking_operations,
    tag_booking_service_sets,
)

NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
SERVICE_A = "11111111-1111-1111-1111-111111111111"
SERVICE_B = "22222222-2222-2222-2222-222222222222"
DOCTOR_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DOCTOR_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DOCTOR_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
BRANCH = "33333333-3333-3333-3333-333333333333"
WORKSPACE = UUID("44444444-4444-4444-4444-444444444444")
PATIENT = UUID("55555555-5555-5555-5555-555555555555")
DAY = date(2026, 9, 14)


def _catalog() -> dict[str, object]:
    return {
        "services": [
            {"id": SERVICE_A, "name": "???", "duration_minutes": 30, "operational_category": "laser"},
            {"id": SERVICE_B, "name": "?????", "duration_minutes": 30, "operational_category": "laser"},
        ],
        "doctors": [
            {"id": DOCTOR_A, "name": "????", "service_ids": [SERVICE_A, SERVICE_B]},
            {"id": DOCTOR_B, "name": "????", "service_ids": [SERVICE_A, SERVICE_B]},
            {"id": DOCTOR_C, "name": "????", "service_ids": [SERVICE_A]},
        ],
    }


def _semantic():
    return build_semantic_context(_catalog())


def _turn() -> TiaTurnUnderstanding:
    doctor_set = EntityReference(text=None, ref=None, candidate_refs=["D1", "D2"], candidate_mode="set")
    return TiaTurnUnderstanding(
        operations=[
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(text=None, ref="S1", candidate_refs=[]),
                    doctor=doctor_set,
                    date=DateConstraint(mode="next_available", start_date=None, end_date=None),
                ),
            ),
            TurnOperation(
                type="book",
                entities=TurnEntities(
                    service=EntityReference(text=None, ref="S2", candidate_refs=[]),
                    doctor=doctor_set,
                    date=DateConstraint(mode="next_available", start_date=None, end_date=None),
                ),
            ),
        ],
        safety_signals=[],
    )


def _slot(service_id: str, doctor_id: str, start: str, end: str) -> AvailabilitySlot:
    return AvailabilitySlot(
        branch_id=BRANCH,
        branch_name="Clinic",
        doctor_id=doctor_id,
        doctor_name="Doctor",
        service_id=service_id,
        service_name="Service",
        start_at=datetime.fromisoformat(start),
        end_at=datetime.fromisoformat(end),
        duration_minutes=30,
        price_minor=10000,
        currency="EGP",
    )


def _result(service_id: str, slots: list[AvailabilitySlot]) -> AvailabilityResult:
    return AvailabilityResult(
        timezone="Africa/Cairo",
        branch_id=BRANCH,
        branch_name="Clinic",
        service_id=service_id,
        service_name="Service",
        service_duration_minutes=30,
        service_price_minor=10000,
        service_currency="EGP",
        slots=tuple(slots),
    )


class _Adapter:
    def get_availability(self, request):
        # Doctor B has the globally earliest joint chain. Doctor A has a later one.
        if request.doctor_id == DOCTOR_B:
            if request.service_id == SERVICE_A:
                return _result(SERVICE_A, [_slot(SERVICE_A, DOCTOR_B, "2026-09-14T07:00:00+00:00", "2026-09-14T07:30:00+00:00")])
            return _result(SERVICE_B, [_slot(SERVICE_B, DOCTOR_B, "2026-09-14T07:30:00+00:00", "2026-09-14T08:00:00+00:00")])
        if request.doctor_id == DOCTOR_A:
            if request.service_id == SERVICE_A:
                return _result(SERVICE_A, [_slot(SERVICE_A, DOCTOR_A, "2026-09-14T09:00:00+00:00", "2026-09-14T09:30:00+00:00")])
            return _result(SERVICE_B, [_slot(SERVICE_B, DOCTOR_A, "2026-09-14T09:30:00+00:00", "2026-09-14T10:00:00+00:00")])
        return _result(request.service_id, [])


class _Db:
    def get(self, _model, _key):
        return SimpleNamespace(workspace_id=WORKSPACE, buffer_before_minutes=0, buffer_after_minutes=0)


def test_multi_service_booking_does_not_ask_customer_to_pick_common_doctor() -> None:
    semantic = _semantic()
    plan = plan_turn(_turn(), PlannerContext(semantic_context=semantic, active_task=None, now=NOW))

    assert len(plan.steps) == 2
    assert all(step.disposition == "read" for step in plan.steps)
    assert all(step.write_intent is not None for step in plan.steps)
    assert all(step.clarification_field is None for step in plan.steps)


def test_same_category_multi_service_visit_collapses_to_one_appointment_contract() -> None:
    semantic = _semantic()
    normalized_turn = merge_same_visit_booking_operations(_turn())
    collapsed, service_sets = collapse_booking_service_sets(
        normalized_turn,
        semantic_context=semantic,
        catalog=_catalog(),
    )
    plan = tag_booking_service_sets(
        plan_turn(
            collapsed,
            PlannerContext(semantic_context=semantic, active_task=None, now=NOW),
        ),
        service_sets=service_sets,
    )

    assert len(collapsed.operations) == 1
    assert service_sets == {0: [SERVICE_A, SERVICE_B]}
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.facts["requested_service_ids"] == [SERVICE_A, SERVICE_B]
    assert step.facts["multi_service_single_appointment"] is True
    assert step.facts["booking_proposal_required"] is True
    booking_writes = [
        item
        for item in plan.steps
        if item.write_intent is not None and item.write_intent.kind == "booking"
    ]
    assert len(booking_writes) <= 1
    if booking_writes:
        assert booking_writes[0].write_intent is not None
        assert booking_writes[0].write_intent.parameters["requested_service_ids"] == [
            SERVICE_A,
            SERVICE_B,
        ]


def test_no_common_doctor_blocks_entire_compound_visit() -> None:
    semantic = _semantic()
    plan = plan_turn(_turn(), PlannerContext(semantic_context=semantic, active_task=None, now=NOW))
    # Force disjoint canonical candidate sets before grouping.
    first, second = plan.steps
    assert first.write_intent is not None and second.write_intent is not None
    first = first.model_copy(update={"write_intent": first.write_intent.model_copy(update={"parameters": {**first.write_intent.parameters, "doctor_ids": [DOCTOR_A]}}), "facts": {**first.facts, "doctor_ids": [DOCTOR_A]}})
    second = second.model_copy(update={"write_intent": second.write_intent.model_copy(update={"parameters": {**second.write_intent.parameters, "doctor_ids": [DOCTOR_B]}}), "facts": {**second.facts, "doctor_ids": [DOCTOR_B]}})
    grouped = normalize_compound_turn_plan(plan.model_copy(update={"steps": [first, second]}), catalog=semantic.model_input)
    context = ReadExecutionContext(
        db=_Db(),
        workspace=SimpleNamespace(id=WORKSPACE, primary_branch_id=UUID(BRANCH)),
        patient=SimpleNamespace(id=PATIENT),
        now=NOW,
        catalog={},
        adapter=_Adapter(),
    )
    resolved = preflight_compound_visit_plan(grouped, context=context, timezone_name="Africa/Cairo")
    assert all(step.write_intent is None for step in resolved.steps)
    assert all(step.disposition == "blocked" for step in resolved.steps)
    assert all(step.facts.get("compound_visit_no_common_doctor") is True for step in resolved.steps)
